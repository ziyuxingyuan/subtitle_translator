#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译引擎
负责调用AI API进行翻译
"""

import json
import logging
import os
import re
import time
import requests
import threading
import sys
from pathlib import Path
from queue import Queue, Empty
from datetime import datetime
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.exceptions import ChunkedEncodingError, ConnectionError
from modules.srt_parser import SRTParser
from modules.api_manager import APIManager
from modules.rate_limiter import RateLimiter
from modules.config_paths import get_target_config_dir


class UserStoppedException(Exception):
    """用户手动停止异常"""
    pass


class EmptyStreamException(Exception):
    """当API返回了空的流式响应时抛出的异常"""
    pass


def debug_print(*args, **kwargs):
    """调试日志输出，统一写入 translation logger。"""
    try:
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "")
        message = sep.join(str(arg) for arg in args) + end
        logging.getLogger("translation").debug(message.rstrip("\n"))
    except Exception:
        return


class TranslationEngine:
    """翻译引擎"""

    _active_lock = threading.Lock()
    _active_sessions = set()
    _active_responses = set()
    _instances_lock = threading.Lock()
    _active_instances = set()

    @classmethod
    def abort_all(cls) -> None:
        # 先广播 stop_event，再强制关闭底层会话/响应，确保“停止翻译”即时生效。
        with cls._instances_lock:
            instances = list(cls._active_instances)
        for engine in instances:
            try:
                stop_event = engine.config.get("stop_event") if isinstance(engine.config, dict) else None
                if stop_event is not None and hasattr(stop_event, "set"):
                    stop_event.set()
            except Exception:
                continue

        with cls._active_lock:
            sessions = list(cls._active_sessions)
            responses = list(cls._active_responses)
        for response in responses:
            try:
                response.close()
            except Exception:
                pass
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass
        with cls._active_lock:
            for response in responses:
                cls._active_responses.discard(response)
            for session in sessions:
                cls._active_sessions.discard(session)

    @classmethod
    def _register_session(cls, session: requests.Session) -> None:
        with cls._active_lock:
            cls._active_sessions.add(session)

    @classmethod
    def _unregister_session(cls, session: requests.Session) -> None:
        with cls._active_lock:
            cls._active_sessions.discard(session)

    @classmethod
    def _register_response(cls, response: requests.Response) -> None:
        with cls._active_lock:
            cls._active_responses.add(response)

    @classmethod
    def _unregister_response(cls, response: requests.Response) -> None:
        with cls._active_lock:
            cls._active_responses.discard(response)

    def __init__(self, shared_rate_limiter: RateLimiter = None):
        self.parser = SRTParser()
        self.api_manager = APIManager()
        # 如果没有提供共享实例，则创建一个新的，以保持向后兼容
        self.rate_limiter = shared_rate_limiter if shared_rate_limiter else RateLimiter()
        self.config = {}
        self.session = requests.Session()
        with self._instances_lock:
            self._active_instances.add(self)

    def configure(self, config: Dict[str, Any]):
        """
        配置翻译引擎

        Args:
            config (Dict[str, Any]): 配置字典
        """
        self.config = config

        # 允许通过配置传入共享的 rate_limiter
        if 'rate_limiter' in config and isinstance(config['rate_limiter'], RateLimiter):
            self.rate_limiter = config['rate_limiter']

        # 设置会话超时
        timeout = config.get('timeout', 30)
        self.session.timeout = timeout

    def _ensure_not_stopped(self) -> None:
        stop_event = self.config.get("stop_event")
        if stop_event and stop_event.is_set():
            raise UserStoppedException("用户已停止翻译。")

    def _wait_with_stop(self, seconds: float) -> None:
        remaining = self._get_request_deadline_remaining()
        if remaining is not None:
            if remaining <= 0:
                raise TimeoutError("当前批次已超过硬超时上限。")
            seconds = min(seconds, remaining)
        stop_event = self.config.get("stop_event")
        if stop_event:
            stop_event.wait(seconds)
            if stop_event.is_set():
                raise UserStoppedException("用户已停止翻译。")
        else:
            time.sleep(seconds)

    def _get_request_deadline_remaining(self) -> Optional[float]:
        raw_deadline = self.config.get("request_deadline")
        if raw_deadline is None:
            return None
        try:
            deadline = float(raw_deadline)
        except (TypeError, ValueError):
            return None
        return deadline - time.monotonic()

    def _resolve_request_timeout(self, default_seconds: int) -> float:
        """解析并规范化单请求硬超时（秒），并受批次 deadline 约束。"""
        raw_timeout = self.config.get("timeout", default_seconds)
        try:
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            timeout_seconds = float(default_seconds)
        timeout_seconds = max(0.1, timeout_seconds)

        remaining = self._get_request_deadline_remaining()
        if remaining is not None:
            if remaining <= 0:
                raise TimeoutError("当前批次已超过硬超时上限。")
            timeout_seconds = min(timeout_seconds, max(0.1, remaining))
        return timeout_seconds

    def translate_batch(self, subtitle_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        根据接口的配置，自动选择流式或非流式方法进行翻译。
        """
        # 从API管理器查询当前接口是否启用了流式传输
        self._ensure_not_stopped()
        use_streaming = self.api_manager.is_streaming_enabled_for_provider(self.config['provider'])

        if use_streaming:
            # 如果启用了流式，调用新的流式方法
            if self.config.get("debug_mode"):
                debug_print(f"[{datetime.now()}] Provider '{self.config['provider']}' is using STREAMING mode.")
            return self._translate_batch_stream(subtitle_blocks)
        else:
            # 否则，调用旧的非流式方法
            if self.config.get("debug_mode"):
                debug_print(f"[{datetime.now()}] Provider '{self.config['provider']}' is using NON-STREAMING mode.")
            return self._translate_batch_non_stream(subtitle_blocks)

    def _translate_batch_non_stream(self, subtitle_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        使用传统的非流式请求进行翻译。
        """
        if not subtitle_blocks:
            return []

        # 提取文本进行翻译
        start_index = 1  # 行号从1开始
        texts_to_translate = self.parser.extract_text_for_translation(subtitle_blocks, start_index)

        if not texts_to_translate:
            raise ValueError("没有找到需要翻译的文本")

        # 调用API进行翻译
        translations = self._call_api(texts_to_translate)

        # 验证翻译结果
        if len(translations) != len(texts_to_translate):
            raise ValueError(f"翻译结果数量({len(translations)})与输入数量({len(texts_to_translate)})不匹配")

        if self.config.get("debug_mode", False):
            batch_index = int(self.config.get("debug_batch_index", 0))
            task_id = self.config.get("debug_task_id") or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            batch_debug_prefix = f"task_{task_id}/batch_{batch_index}"
            self._log_to_debug_file(batch_debug_prefix, "=== 输入文本 ===", "\n".join(texts_to_translate), "a")
            self._log_to_debug_file(batch_debug_prefix, "=== 翻译结果 ===", "\n".join(translations), "a")
            compare = self._format_translation_compare(texts_to_translate, translations)
            self._log_to_debug_file(batch_debug_prefix, "=== 处理前后对比 ===", compare, "a")

        # 合并翻译结果
        translated_blocks = self.parser.merge_translations(subtitle_blocks, translations)

        return translated_blocks

    def _translate_batch_stream(self, subtitle_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        使用流式请求进行翻译，并实时组装结果。
        这能有效避免网关超时 (504)。
        """
        if not subtitle_blocks:
            return []

        # 提取文本进行翻译
        start_index = 1  # 行号从1开始
        texts_to_translate = self.parser.extract_text_for_translation(subtitle_blocks, start_index)

        if not texts_to_translate:
            raise ValueError("没有找到需要翻译的文本")

        # 调用流式API进行翻译
        translations = self._call_api_stream(texts_to_translate)

        # 验证翻译结果
        if len(translations) != len(texts_to_translate):
            raise ValueError(f"翻译结果数量({len(translations)})与输入数量({len(texts_to_translate)})不匹配")

        if self.config.get("debug_mode", False):
            batch_index = int(self.config.get("debug_batch_index", 0))
            task_id = self.config.get("debug_task_id") or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            batch_debug_prefix = f"task_{task_id}/batch_{batch_index}"
            self._log_to_debug_file(batch_debug_prefix, "=== 输入文本 ===", "\n".join(texts_to_translate), "a")
            self._log_to_debug_file(batch_debug_prefix, "=== 翻译结果 ===", "\n".join(translations), "a")
            compare = self._format_translation_compare(texts_to_translate, translations)
            self._log_to_debug_file(batch_debug_prefix, "=== 处理前后对比 ===", compare, "a")

        # 合并翻译结果
        translated_blocks = self.parser.merge_translations(subtitle_blocks, translations)

        return translated_blocks

    def translate_batch_concurrent(self, subtitle_blocks: List[Dict[str, Any]],
                                 concurrency: int = 0) -> List[Dict[str, Any]]:
        """
        并发翻译一批字幕

        Args:
            subtitle_blocks (List[Dict[str, Any]]): 字幕块列表
            concurrency (int): 并发数，0表示自动模式

        Returns:
            List[Dict[str, Any]]: 翻译后的字幕块列表
        """
        if not subtitle_blocks:
            return []

        # 如果并发数为1或者字幕块很少，使用单线程模式
        if concurrency <= 1 or len(subtitle_blocks) <= 5:
            return self.translate_batch(subtitle_blocks)

        # 获取有效的并发数
        from modules.config_manager import ConfigManager
        config_manager = ConfigManager()
        provider = self.config.get('provider', 'openai')
        effective_concurrency = config_manager.get_effective_concurrency(provider, concurrency)

        print(f"开始并发翻译，并发数: {effective_concurrency}")

        # 从配置中获取用户设置的批次大小
        user_batch_size = self.config.get('batch_size', 20)

        # 按照用户设置的批次大小分割字幕块
        batches = []
        for i in range(0, len(subtitle_blocks), user_batch_size):
            batches.append(subtitle_blocks[i:i + user_batch_size])

        print(f"分割成 {len(batches)} 个批次，每批 {user_batch_size} 个字幕块（用户设置）")

        # 使用线程池并发翻译
        results = []
        with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
            # 提交所有翻译任务
            future_to_index = {
                executor.submit(self._translate_single_batch, batch): i
                for i, batch in enumerate(batches)
            }

            # 收集结果
            for future in as_completed(future_to_index):
                batch_index = future_to_index[future]
                try:
                    result = future.result()
                    results.append((batch_index, result))
                    print(f"批次 {batch_index + 1}/{len(batches)} 完成")
                except Exception as e:
                    print(f"批次 {batch_index + 1} 翻译失败: {str(e)}")
                    # 失败时使用单线程重试
                    try:
                        result = self.translate_batch(batches[batch_index])
                        results.append((batch_index, result))
                        print(f"批次 {batch_index + 1} 重试成功")
                    except Exception as retry_error:
                        print(f"批次 {batch_index + 1} 重试也失败: {str(retry_error)}")
                        raise Exception(f"批次 {batch_index + 1} 翻译失败: {str(e)}")

        # 按批次索引重新排序结果
        results.sort(key=lambda x: x[0])

        # 合并所有批次的结果
        final_blocks = []
        for _, batch_result in results:
            final_blocks.extend(batch_result)

        print(f"并发翻译完成，共处理 {len(final_blocks)} 个字幕块")
        return final_blocks

    def _translate_single_batch(self, subtitle_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        翻译单个批次（线程安全）

        Args:
            subtitle_blocks (List[Dict[str, Any]]): 字幕块列表

        Returns:
            List[Dict[str, Any]]: 翻译后的字幕块列表
        """
        # 为每个线程创建独立的会话和配置
        thread_config = self.config.copy()

        # 为每个线程创建独立的会话和配置，但共享同一个速率限制器
        thread_engine = TranslationEngine(shared_rate_limiter=self.rate_limiter)
        thread_engine.configure(thread_config)

        return thread_engine.translate_batch(subtitle_blocks)

    def _call_api(self, texts: List[str]) -> List[str]:
        """
        调用API进行翻译

        Args:
            texts (List[str]): 待翻译的文本列表

        Returns:
            List[str]: 翻译结果列表
        """
        max_retries = self.config.get('max_retries', 3)
        last_error = None

        for attempt in range(max_retries + 1):
            self._ensure_not_stopped()
            try:
                if attempt > 0:
                    # 指数退避
                    wait_time = min(2 ** attempt, 30)  # 最多等待30秒
                    print(f"第{attempt + 1}次重试，等待{wait_time}秒...")
                    self._wait_with_stop(wait_time)

                self._ensure_not_stopped()
                result = self._make_api_request(texts)
                return result

            except UserStoppedException:
                raise
            except Exception as e:
                last_error = e
                error_message = str(e)
                if self.config.get("stop_event") and self.config["stop_event"].is_set():
                    raise UserStoppedException("用户已停止翻译。") from e
                print(f"API调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {error_message}")

                # 如果是认证错误，不需要重试
                if self._is_auth_error(error_message):
                    break

                # 如果是速率限制错误，使用智能等待而非简单重试
                if self._is_rate_limit_error(error_message):
                    if attempt < max_retries:
                        print(f"检测到速率限制错误，进行智能等待...")
                        # RateLimiter已经记录了错误并设置了等待时间
                        # 这里使用更长的等待时间作为后备
                        wait_time = min(60, 2 ** attempt * 10)  # 最多等待60秒
                        print(f"等待{wait_time}秒后重试...")
                        self._wait_with_stop(wait_time)
                        continue
                    else:
                        break

                # 如果是行数不匹配错误，增加重试次数
                if "翻译结果行数" in error_message and "不匹配" in error_message:
                    if attempt < max_retries:
                        print(f"检测到行数不匹配，调整后重试...")
                        continue

        raise Exception(f"API调用失败，已重试{max_retries}次。最后错误: {str(last_error)}")

    def _call_api_stream(self, texts: List[str]) -> List[str]:
        """
        使用流式请求调用API进行翻译，并实时组装结果。
        这能有效避免网关超时 (504)。
        """
        max_retries = self.config.get('max_retries', 3)
        last_error = None

        for attempt in range(max_retries + 1):
            self._ensure_not_stopped()
            try:
                if attempt > 0:
                    # 指数退避
                    wait_time = min(2 ** attempt, 30)  # 最多等待30秒
                    print(f"第{attempt + 1}次重试，等待{wait_time}秒...")
                    self._wait_with_stop(wait_time)

                self._ensure_not_stopped()
                result = self._make_api_request_stream(texts)
                return result

            except UserStoppedException as e:
                print(f"任务已被用户停止: {e}")
                raise e

            # --- 新增：捕获流式超时和空流异常 ---
            except (EmptyStreamException, TimeoutError) as e:
                last_error = e
                if self.config.get("stop_event") and self.config["stop_event"].is_set():
                    raise UserStoppedException("用户已停止翻译。") from e
                print(f"API流式调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
                # 如果还有重试次数，则继续循环进行重试
                if attempt < max_retries:
                    wait_time = min(2 ** (attempt + 1), 30)
                    print(f"等待 {wait_time} 秒后重试...")
                    self._wait_with_stop(wait_time)
                    continue
                # 如果没有重试次数了，则在循环外抛出最终异常

            except Exception as e:
                last_error = e
                error_message = str(e)
                if self.config.get("stop_event") and self.config["stop_event"].is_set():
                    raise UserStoppedException("用户已停止翻译。") from e
                print(f"API流式调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {error_message}")

                # 如果是认证错误，不需要重试
                if self._is_auth_error(error_message):
                    break

                # 如果是速率限制错误，使用智能等待而非简单重试
                if self._is_rate_limit_error(error_message):
                    if attempt < max_retries:
                        print(f"检测到速率限制错误，进行智能等待...")
                        # RateLimiter已经记录了错误并设置了等待时间
                        # 这里使用更长的等待时间作为后备
                        wait_time = min(60, 2 ** attempt * 10)  # 最多等待60秒
                        print(f"等待{wait_time}秒后重试...")
                        self._wait_with_stop(wait_time)
                        continue
                    else:
                        break

                # 如果是行数不匹配错误，增加重试次数
                if "翻译结果行数" in error_message and "不匹配" in error_message:
                    if attempt < max_retries:
                        print(f"检测到行数不匹配，调整后重试...")
                        continue

        raise Exception(f"API流式调用失败，已重试{max_retries}次。最后错误: {str(last_error)}")

    def _make_api_request_stream(self, texts: List[str]) -> List[str]:
        """
        发起流式API请求，并使用双线程解耦架构，彻底解决流式僵局问题。
        这是V20.0 架构最终修复版。
        """
        # --- 1. 准备工作 (不变) ---
        self._ensure_not_stopped()
        batch_index = int(self.config.get("debug_batch_index", 0))
        task_id = self.config.get("debug_task_id") or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        batch_debug_prefix = f"task_{task_id}/batch_{batch_index}"
        provider = self.config.get('provider')
        api_key = self.config.get('api_key')
        endpoint = self.config.get('endpoint')
        model = self.config.get('model')
        debug_mode = self.config.get('debug_mode', False)

        # 验证必要参数
        if not all([provider, api_key, endpoint, model]):
            raise ValueError("缺少必要的API配置参数")

        # 获取速率限制配置
        rate_limits = self.config.get("provider_limits", {})

        system_prompt = self._build_system_prompt(
            self.config.get('source_language', '日文'),
            self.config.get('target_language', '中文')
        )
        user_message = '\n'.join(texts)

        # 估算token使用量
        input_tokens = self._estimate_input_tokens(texts, system_prompt)
        estimated_output_tokens = self._estimate_output_tokens(texts)
        total_estimated_tokens = input_tokens + estimated_output_tokens

        rate_limit_log = self.config.get("rate_limit_log")
        if rate_limit_log and not callable(rate_limit_log):
            rate_limit_log = None

        # 使用速率限制器进行等待并预占请求次数
        if not self.rate_limiter.reserve_request(
            provider,
            rate_limits,
            total_estimated_tokens,
            stop_event=self.config.get("stop_event"),
            log_func=rate_limit_log,
        ):
            raise Exception("速率限制检查失败")
        self._ensure_not_stopped()

        headers = self.api_manager.get_auth_headers(provider, api_key)
        headers['Content-Type'] = 'application/json'
        headers['User-Agent'] = 'SubtitleTranslator/1.0'
        request_data = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}],
            "temperature": 0.2,
            "max_tokens": self._calculate_max_tokens(user_message),
            "stream": True
        }
        chat_endpoint = f"{endpoint.rstrip('/')}/chat/completions"
        stop_event = self.config.get('stop_event')
        stream_timeout = self._resolve_request_timeout(300)
        request_deadline = time.monotonic() + stream_timeout

        # 记录发送的完整请求（调试模式）
        formatted_request = ""
        if debug_mode:
            header = (
                f"=== 批次 {batch_index + 1} 调试日志 (Task: {task_id}) ===\n"
                f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            self._log_to_debug_file(batch_debug_prefix, "", header, "w")
            formatted_request = self._format_request_data(request_data, headers, provider, model)
            # 立即写入请求部分
            self._log_to_debug_file(batch_debug_prefix, "=== 发送的完整流式请求 ===", formatted_request, 'a')

            # 在控制台显示调试信息
            debug_print(f"\n=== 调试模式：流式翻译请求信息 ===")
            debug_print(f"AI接口: {provider}")
            debug_print(f"模型: {model}")
            debug_print(f"预估输入tokens: {input_tokens}, 预估输出tokens: {estimated_output_tokens}")
            debug_print(f"使用max_tokens: {self._calculate_max_tokens(user_message)}")
            debug_print(f"使用流式传输: True")
            debug_print(f"流式单请求硬超时: {stream_timeout}秒")
            debug_print(f"\n--- 系统提示词 ---")
            debug_print(system_prompt)
            debug_print(f"\n--- 用户输入（前500字符）---")
            debug_print(user_message[:500] + "..." if len(user_message) > 500 else user_message)
            debug_print("=" * 60)

        # 代理设置
        proxy_enabled = self.config.get('proxy_enabled', False)
        proxy_address = self.config.get('proxy_address', '').strip()

        proxies = None
        if proxy_enabled and proxy_address:
            # 如果开关打开，且地址不为空，则强制使用该代理
            proxies = {'http': proxy_address, 'https': proxy_address}
            if debug_mode: debug_print(f"代理已启用，使用自定义代理: {proxy_address}")
        else:
            # 否则（开关关闭 或 地址为空），强制禁用所有代理
            proxies = {'http': None, 'https': None}
            if debug_mode: debug_print("代理已禁用，强制直连（忽略系统代理）。")

        # --- 2. [V20.0 核心重构] 创建双线程架构 ---

        # 共享数据结构：一个用于网络原始数据的队列，一个用于最终内容的变量
        raw_queue = Queue()
        shared_data = {'content': "", 'lock': threading.Lock()}

        # 定义"卸货员"线程：只负责从网络读取原始数据并放入队列
        def network_reader_thread(response):
            try:
                for line in response.iter_lines():
                    if stop_event and stop_event.is_set():
                        break
                    if line:
                        raw_queue.put(line)
            except Exception as e:
                # 将网络错误也放入队列，以便主线程捕获
                raw_queue.put(e)
            finally:
                # 发送结束信号
                raw_queue.put(None)

        # 定义"日志记录员"线程 (不变)
        log_thread_stop_event = threading.Event()
        def _log_stream_progress():
            """后台线程，定时将接收到的内容快照【追加】到调试文件。"""
            debug_file = self._get_debug_file_path(batch_debug_prefix)

            while not log_thread_stop_event.wait(0.5): # 更快响应停止
                if log_thread_stop_event.is_set():
                    break
                if stop_event and stop_event.is_set():
                    break

                with shared_data['lock']:
                    current_content = shared_data['content']

                if current_content:
                    try:
                        # [V17.0 核心修正] 使用 'a' (追加) 模式写入
                        with open(debug_file, 'a', encoding='utf-8') as f:
                            snapshot_header = f"\n\n=== 响应快照 @ {datetime.now().strftime('%H:%M:%S')} ===\n"
                            f.write(snapshot_header)
                            f.write("=" * (len(snapshot_header) - 2) + "\n")
                            f.write(current_content)

                            # --- [V18.0 核心修正] 强制刷新缓冲区 ---
                            # 1. 强制将Python的缓冲区内容写入到OS缓存
                            f.flush()
                            # 2. 强制将OS缓存的内容写入到物理磁盘
                            os.fsync(f.fileno())
                    except Exception as e:
                        print(f"[日志线程错误] 无法追加写入并刷新调试文件: {e}")

        # --- 3. 执行请求并启动线程 ---
        response = None
        log_thread = None

        self._register_session(self.session)
        try:
            response = self.session.post(
                chat_endpoint, json=request_data, headers=headers, stream=True,
                proxies=proxies,  # [V14.0] 使用配置的代理设置
                timeout=(min(30, stream_timeout), stream_timeout) # (连接超时, 读取超时) - 内层防护
            )
            self._register_response(response)
            response.raise_for_status()

            # 启动"卸货员"和"日志记录员"
            reader = threading.Thread(target=network_reader_thread, args=(response,), daemon=True)
            reader.start()

            if debug_mode:
                log_thread = threading.Thread(target=_log_stream_progress, daemon=True)
                log_thread.start()

            debug_print(f"正在调用流式API: {provider} - {model}")
            debug_print(f"输入文本行数: {len(texts)}")
            debug_print(f"预估输入tokens: {input_tokens}, 预估输出tokens: {estimated_output_tokens}")

            # --- 4. [V20.0 核心重构] 主线程成为"拆包员" ---
            # 主线程现在只从队列中获取数据并处理，不再直接接触网络
            while True:
                # 从队列中获取一个原始行，如果队列为空会阻塞等待
                self._ensure_not_stopped()
                remaining = request_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"单次流式请求超过硬超时上限（{stream_timeout}秒）")
                try:
                    line_bytes = raw_queue.get(timeout=max(0.05, min(0.5, remaining)))
                except Empty:
                    continue

                # 检查结束信号或错误
                if line_bytes is None:
                    break # "卸货员"已完成工作
                if isinstance(line_bytes, Exception):
                    if stop_event and stop_event.is_set():
                        raise UserStoppedException("用户已停止翻译。")
                    raise line_bytes # 重新抛出网络错误

                # 解码和处理
                line_str = line_bytes.decode('utf-8')
                if line_str.startswith('data: '):
                    json_str = line_str[6:].strip()
                    if json_str == '[DONE]' or json_str == '':
                        continue
                    try:
                        chunk_data = json.loads(json_str)
                        content = chunk_data.get('choices', [{}])[0].get('delta', {}).get('content')
                        if content:
                            # 更新共享数据，供日志线程和最终结果使用
                            with shared_data['lock']:
                                shared_data['content'] += content
                    except (json.JSONDecodeError, IndexError):
                        continue

                # 智能截断逻辑 (不变)
                with shared_data['lock']:
                    current_full_content = shared_data['content']
                try:
                    valid_lines = re.findall(r'^\s*\d+\s*[:.]\s*', current_full_content, re.MULTILINE)
                    if len(valid_lines) >= len(texts):
                        if debug_mode:
                            debug_print(f"智能截断: 已收到所有 {len(texts)} 行翻译，提前结束流接收。")
                        break
                except Exception:
                    pass

        finally:
            # --- 5. 资源清理 (不变) ---
            if response:
                response.close()
                self._unregister_response(response)
            self._unregister_session(self.session)
            if log_thread:
                log_thread_stop_event.set()
                log_thread.join(timeout=2)

            # 写入最终日志 (不变)
            if debug_mode and not (stop_event and stop_event.is_set()):
                with shared_data['lock']:
                    final_content = shared_data['content']
                final_log_section = (
                    f"\n\n=== 最终收到的完整响应 (Final & Clean) ===\n{'='*45}\n"
                    f"{final_content}"
                )
                # 使用 self._log_to_debug_file，并确保它使用追加模式
                self._log_to_debug_file(batch_debug_prefix, "", final_log_section, 'a') # <-- 使用 'a'
                if debug_mode: debug_print(f"最终调试日志已追加写入: {batch_debug_prefix}_api_interaction.txt")

        # --- 6. 最终检查与解析 (不变) ---
        with shared_data['lock']:
            full_response_content = shared_data['content']

        if not full_response_content.strip():
            raise EmptyStreamException("API返回了空的流式响应。")

        debug_print(f"\n=== 调试模式：流式AI翻译结果 ===")
        debug_print(f"完整流式响应:\n{full_response_content}")
        debug_print("=" * 50)

        translations = self.parser.parse_translation_response(full_response_content, len(texts))
        self.rate_limiter.record_tokens(provider, input_tokens, self._estimate_output_tokens(texts))
        debug_print(f"流式翻译成功，输出{len(translations)}行")
        return translations

    def _make_api_request(self, texts: List[str]) -> List[str]:
        """
        发起API请求

        Args:
            texts (List[str]): 待翻译的文本列表

        Returns:
            List[str]: 翻译结果列表
        """
        # 生成唯一的批次调试ID
        self._ensure_not_stopped()
        batch_index = int(self.config.get("debug_batch_index", 0))
        task_id = self.config.get("debug_task_id") or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        batch_debug_prefix = f"task_{task_id}/batch_{batch_index}"

        provider = self.config.get('provider')
        api_key = self.config.get('api_key')
        endpoint = self.config.get('endpoint')
        model = self.config.get('model')
        debug_mode = self.config.get('debug_mode', False)
        source_lang = self.config.get('source_language', '日文')
        target_lang = self.config.get('target_language', '中文')

        # 验证必要参数
        if not all([provider, api_key, endpoint, model]):
            raise ValueError("缺少必要的API配置参数")

        # 获取速率限制配置 (从config中获取，而不是重新调用api_manager)
        rate_limits = self.config.get("provider_limits", {})

        # 构建系统提示词
        system_prompt = self._build_system_prompt(source_lang, target_lang)

        # 估算token使用量
        input_tokens = self._estimate_input_tokens(texts, system_prompt)
        estimated_output_tokens = self._estimate_output_tokens(texts)
        total_estimated_tokens = input_tokens + estimated_output_tokens

        rate_limit_log = self.config.get("rate_limit_log")
        if rate_limit_log and not callable(rate_limit_log):
            rate_limit_log = None

        # 使用速率限制器进行等待并预占请求次数（如果需要）
        if not self.rate_limiter.reserve_request(
            provider,
            rate_limits,
            total_estimated_tokens,
            stop_event=self.config.get("stop_event"),
            log_func=rate_limit_log,
        ):
            raise Exception("速率限制检查失败")
        self._ensure_not_stopped()

        # 构建用户消息
        user_message = '\n'.join(texts)

        # 构建请求头
        headers = self.api_manager.get_auth_headers(provider, api_key)
        headers['Content-Type'] = 'application/json'

        # 添加User-Agent（帮助绕过一些简单的CF盾）
        headers['User-Agent'] = 'SubtitleTranslator/1.0 (https://github.com/subtitle-translator)'

        # 计算max_tokens
        max_tokens = self._calculate_max_tokens(user_message)

        # 构建请求体
        request_data = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False
        }

        # 记录发送的完整请求（调试模式）
        if self.config.get('debug_mode', False):
            header = (
                f"=== 批次 {batch_index + 1} 调试日志 (Task: {task_id}) ===\n"
                f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            self._log_to_debug_file(batch_debug_prefix, "", header, 'w')
            formatted_request = self._format_request_data(request_data, headers, provider, model)
            self._log_to_debug_file(batch_debug_prefix, "=== 发送的完整请求 ===", formatted_request, 'a')

            # 在控制台显示调试信息
            debug_print(f"\n=== 调试模式：翻译请求信息 ===")
            debug_print(f"AI接口: {provider}")
            debug_print(f"模型: {model}")
            debug_print(f"预估输入tokens: {input_tokens}, 预估输出tokens: {estimated_output_tokens}")
            debug_print(f"使用max_tokens: {max_tokens}")
            debug_print(f"\n--- 系统提示词 ---")
            debug_print(system_prompt)
            debug_print(f"\n--- 用户输入（前500字符）---")
            debug_print(user_message[:500] + "..." if len(user_message) > 500 else user_message)
            debug_print("=" * 60)

        # 发送请求
        chat_endpoint = f"{endpoint.rstrip('/')}/chat/completions"

        debug_print(f"正在调用API: {provider} - {model}")
        debug_print(f"输入文本行数: {len(texts)}")
        debug_print(f"预估输入tokens: {input_tokens}, 预估输出tokens: {estimated_output_tokens}")

        # --- [V14.0] 实现基于勾选框的代理绝对控制逻辑 ---
        proxy_enabled = self.config.get('proxy_enabled', False)
        proxy_address = self.config.get('proxy_address', '').strip()

        proxies = None
        if proxy_enabled and proxy_address:
            # 如果开关打开，且地址不为空，则强制使用该代理
            proxies = {'http': proxy_address, 'https': proxy_address}
            if debug_mode: debug_print(f"代理已启用，使用自定义代理: {proxy_address}")
        else:
            # 否则（开关关闭 或 地址为空），强制禁用所有代理
            proxies = {'http': None, 'https': None}
            if debug_mode: debug_print("代理已禁用，强制直连（忽略系统代理）。")

        self._register_session(self.session)
        response = None
        try:
            request_timeout = self._resolve_request_timeout(60)
            request_started_at = time.monotonic()
            response = self.session.post(
                chat_endpoint,
                json=request_data,
                headers=headers,
                proxies=proxies,
                timeout=(min(30, request_timeout), request_timeout),
            )  # [V14.0] 使用配置的代理设置
            self._register_response(response)
            elapsed = time.monotonic() - request_started_at
            if elapsed > request_timeout:
                raise TimeoutError(f"单次请求超过硬超时上限（{request_timeout}秒）")

            # 处理响应
            self._ensure_not_stopped()
            if response.status_code == 200:
                response_data = response.json()

                if 'choices' in response_data and len(response_data['choices']) > 0:
                    content = response_data['choices'][0]['message']['content']

                    # 记录收到的原始回复（调试模式）
                    if self.config.get('debug_mode', False):
                        formatted_response = str(response_data)  # 简化显示，避免json导入问题
                        self._log_to_debug_file(batch_debug_prefix, "=== 收到的原始回复 ===", formatted_response)

                        # 在控制台显示返回结果
                        ai_response = response_data['choices'][0]['message']['content']
                        debug_print(f"\n=== 调试模式：AI翻译结果 ===")
                        debug_print(f"AI接口: {provider}")
                        debug_print(f"模型: {model}")
                        debug_print(f"翻译结果:\n{ai_response}")
                        debug_print("=" * 50)

                    # 解析翻译结果
                    translations = self.parser.parse_translation_response(content, len(texts))

                    # 计算实际使用的token（如果响应中有usage信息）
                    actual_output_tokens = self._extract_output_tokens(response_data)

                    # 记录成功的API请求
                    self.rate_limiter.record_tokens(provider, input_tokens, actual_output_tokens)

                    debug_print(f"翻译成功，输出{len(translations)}行")
                    debug_print(f"实际使用tokens - 输入: {input_tokens}, 输出: {actual_output_tokens}")
                    return translations
                else:
                    raise Exception("API响应格式异常：缺少choices字段")
            else:
                # 处理错误响应
                error_info = self._parse_api_error(response)

                # 记录API错误（调试模式）
                if self.config.get('debug_mode', False):
                    error_content = f"HTTP Status Code: {response.status_code}\n"
                    error_content += f"Error Info: {error_info}\n"
                    error_content += f"Raw Response: {response.text[:2000]}"  # 限制长度避免文件过大
                    self._log_to_debug_file(batch_debug_prefix, "=== API错误信息 ===", error_content)

                # 检查是否是速率限制错误
                if self._is_rate_limit_error(error_info):
                    retry_after = self._extract_retry_after(response)
                    self.rate_limiter.record_rate_limit_error(provider, error_info, retry_after)

                raise Exception(f"API请求失败: {error_info}")
        finally:
            if response:
                try:
                    response.close()
                except Exception:
                    pass
                self._unregister_response(response)
            self._unregister_session(self.session)

    def _build_system_prompt(self, source_lang: str, target_lang: str) -> str:
        """
        构建系统提示词

        Args:
            source_lang (str): 源语言
            target_lang (str): 目标语言

        Returns:
            str: 系统提示词
        """
        # 检查是否有自定义系统提示词
        custom_prompt = self.config.get('system_prompt', '')

        if custom_prompt:
            # 使用自定义系统提示词，替换其中的占位符
            return custom_prompt.format(
                source_lang=source_lang,
                target_lang=target_lang
            )
        else:
            # 使用默认的系统提示词
            return f"""
你是一个专业的、严格遵守格式的{source_lang}翻译引擎。你的任务是将{source_lang}字幕文件文本翻译成{target_lang}，同时严格保持输入和输出的行数一一对应。

### 核心铁律（最高优先级规则）
1. 输入和输出的格式必须完全相同。如果输入是 "行号: 文本"，输出也必须是 "行号: 译文"。
2. 严禁合并或拆分行：即使原文的多行在语义上是一个完整的句子，你也必须在最终输出时保持独立的行。
3. 失败后果：你的输出将被一个自动化脚本按行号进行匹配。任何行数的变动都会导致整个流程失败。你必须模拟这个脚本的行为，对每一行进行独立处理和输出。

### 翻译流程
请在你的"内心"或"草稿区"按照以下三步思考，但最终只输出严格遵守【核心铁律】的格式化结果。

第一步：内部直译
将输入的每一行{source_lang}文本在内部进行初步的逐行直译。

第二步：内部校正与风格定义
针对每一句初步译文，可以从语义与语境、专业术语、上下文信息、翻译风格、故事背景、人物设定等等方面出发，进行深入分析和校正。
语言风格: 定义为地道的{target_lang}母语者日常口语风格，避免书面语和机器翻译痕迹。
语气情感: 略微非正式，传达热情和真诚的赞赏之情，但避免过多的语气助词和符号（如...和多个!!）。
表达技巧: 思考如何融入地道的{target_lang}俗语和口语化表达（如"压榨"、"忍痛割爱"等），使译文生动，贴近真实对话，但是不要用过多的...符号。
翻译策略: 避免生硬直译，理解每行原文的核心意思和情感。如果一行中包含英文，则将英文部分删除，不翻译也不输出。
译文目标: 高度自然地道的{target_lang}口语译文，如同真诚用户热情推荐，而非机器翻译。

第三步：最终输出生成
整合以上思考，为输入的每一行生成最终的、独立的译文。
即使上一行和下一行在逻辑上是连续的，也要强制将它们的译文分在两行输出。

### 示例
输入：
101: こんにちは
102: 元気ですか？
103: 今日は良い天気ですね

输出：
101: 你好
102: 你好吗？
103: 今天天气真好呢

请严格按照以上规则进行翻译，确保每一行都有对应的翻译结果。
"""

    def _estimate_input_tokens(self, texts: List[str], system_prompt: str) -> int:
        """
        估算输入token数量

        Args:
            texts (List[str]): 待翻译文本列表
            system_prompt (str): 系统提示词

        Returns:
            int: 估算的输入token数量
        """
        # 合并所有文本
        all_text = system_prompt + '\n'.join(texts)

        # 基于字符数的token估算（更精确的方法）
        # 英文字母和数字：约1字符 = 1token
        # 中文字符：约1字符 = 1.5-2tokens
        # 标点和空格：约1字符 = 0.5-1token

        chinese_chars = len([c for c in all_text if '\u4e00' <= c <= '\u9fff'])
        english_chars = len([c for c in all_text if c.isascii() and not c.isspace()])
        spaces = len([c for c in all_text if c.isspace()])
        other_chars = len(all_text) - chinese_chars - english_chars - spaces

        estimated_tokens = (
            chinese_chars * 1.8 +      # 中文字符
            english_chars * 1.0 +      # 英文字符
            spaces * 0.7 +             # 空格
            other_chars * 1.2          # 其他字符（标点等）
        )

        # 为格式化开销预留20%
        return int(estimated_tokens * 1.2)

    def _estimate_output_tokens(self, texts: List[str]) -> int:
        """
        估算输出token数量

        Args:
            texts (List[str]): 输入文本列表

        Returns:
            int: 估算的输出token数量
        """
        # 统计输入文本的字符数
        total_chars = sum(len(text) for text in texts)

        # 根据语言对进行不同的估算
        source_lang = self.config.get('source_language', '日文')
        target_lang = self.config.get('target_language', '中文')

        # 翻译通常会使文本长度发生变化
        if source_lang == '日文' and target_lang == '中文':
            # 日文翻译为中文通常长度略有增加
            length_ratio = 1.2
        elif source_lang == '中文' and target_lang == '日文':
            # 中文翻译为日文通常长度略有减少
            length_ratio = 0.9
        elif '中文' in [source_lang, target_lang] and '英文' in [source_lang, target_lang]:
            # 中英文互译通常长度变化较大
            length_ratio = 1.3
        else:
            # 默认估算
            length_ratio = 1.1

        # 估算输出字符数
        estimated_output_chars = total_chars * length_ratio

        # 转换为token数（输出主要是中文）
        estimated_tokens = int(estimated_output_chars * 1.5)

        return estimated_tokens

    def _extract_output_tokens(self, response_data: Dict[str, Any]) -> int:
        """
        从API响应中提取实际使用的输出token数

        Args:
            response_data (Dict[str, Any]): API响应数据

        Returns:
            int: 实际输出token数
        """
        try:
            # 尝试从usage字段获取
            if 'usage' in response_data:
                usage = response_data['usage']
                if 'completion_tokens' in usage:
                    return usage['completion_tokens']
        except (KeyError, TypeError):
            pass

        # 如果没有usage信息，使用估算
        return 0

    def _is_rate_limit_error(self, error_message: str) -> bool:
        """
        判断是否是速率限制错误

        Args:
            error_message (str): 错误信息

        Returns:
            bool: 是否是速率限制错误
        """
        error_lower = error_message.lower()

        # 常见的速率限制错误关键词
        rate_limit_keywords = [
            'rate limit',
            'rate_limit',
            'too many requests',
            'request limit',
            'token limit',
            'tokens per minute',
            'tpm',
            'requests per minute',
            'rpm',
            'frequency limit',
            'quota exceeded',
            'maximum requests',
            'maximum tokens',
            'usage limit',
            'try again in',
            'please try again',
            'retry after'
        ]

        return any(keyword in error_lower for keyword in rate_limit_keywords)

    def _extract_retry_after(self, response: requests.Response) -> Optional[int]:
        """
        从响应中提取重试等待时间

        Args:
            response (requests.Response): HTTP响应

        Returns:
            Optional[int]: 重试等待时间（秒）
        """
        # 首先检查Retry-After头部
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            try:
                return int(retry_after)
            except ValueError:
                pass

        # 然后尝试从响应体中解析
        try:
            response_data = response.json()
            error_message = response_data.get('error', {}).get('message', '')

            # 使用rate_limiter的解析方法
            return self.rate_limiter._parse_retry_after(error_message)
        except (ValueError, KeyError, AttributeError):
            pass

        return None

    def _calculate_max_tokens(self, text: str) -> int:
        """
        计算所需的最大token数

        Args:
            text (str): 输入文本

        Returns:
            int: 最大token数
        """
        provider = self.config.get('provider')

        # 获取自定义接口设置的max_tokens
        provider_info = self.api_manager.get_provider_info(provider)
        if provider_info and 'max_tokens' in provider_info:
            max_tokens = provider_info['max_tokens']
            debug_print(f"使用自定义接口max_tokens: {max_tokens}")
            return max_tokens

        # 如果没有设置，使用默认值8196
        debug_print(f"使用默认max_tokens: 8196")
        return 8196

    def _parse_api_error(self, response: requests.Response) -> str:
        """
        解析API错误响应

        Args:
            response (requests.Response): HTTP响应

        Returns:
            str: 错误信息
        """
        try:
            error_data = response.json()

            if 'error' in error_data:
                error_info = error_data['error']
                if isinstance(error_info, dict):
                    message = error_info.get('message', '未知错误')
                    error_type = error_info.get('type', '')
                    return f"{error_type}: {message}" if error_type else message
                else:
                    return str(error_info)
            else:
                return f"HTTP {response.status_code}: {response.text[:200]}"
        except:
            return f"HTTP {response.status_code}: {response.text[:200]}"

    def _is_auth_error(self, error_message: str) -> bool:
        """
        判断是否为认证错误

        Args:
            error_message (str): 错误消息

        Returns:
            bool: 是否为认证错误
        """
        auth_indicators = [
            '401', 'unauthorized', 'authentication failed',
            'invalid api key', 'forbidden', '403'
        ]

        error_lower = error_message.lower()
        return any(indicator in error_lower for indicator in auth_indicators)

    def estimate_translation_time(self, text_count: int, batch_size: int) -> Dict[str, Any]:
        """
        估算翻译时间

        Args:
            text_count (int): 文本行数
            batch_size (int): 批次大小

        Returns:
            Dict[str, Any]: 时间估算信息
        """
        # 基于经验值的估算
        avg_response_time = 3  # 平均每个API请求3秒
        batches = (text_count + batch_size - 1) // batch_size

        estimated_seconds = batches * avg_response_time
        estimated_minutes = estimated_seconds / 60

        return {
            'text_count': text_count,
            'batch_size': batch_size,
            'batch_count': batches,
            'estimated_seconds': estimated_seconds,
            'estimated_minutes': round(estimated_minutes, 1),
            'estimated_formatted': self._format_duration(estimated_seconds)
        }

    def _format_duration(self, seconds: float) -> str:
        """
        格式化时间显示

        Args:
            seconds (float): 秒数

        Returns:
            str: 格式化的时间字符串
        """
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"

    def get_translation_statistics(self, original_blocks: List[Dict[str, Any]],
                                 translated_blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        获取翻译统计信息

        Args:
            original_blocks (List[Dict[str, Any]]): 原始字幕块
            translated_blocks (List[Dict[str, Any]]): 翻译后的字幕块

        Returns:
            Dict[str, Any]: 统计信息
        """
        if not original_blocks or not translated_blocks:
            return {}

        # 字符统计
        original_chars = sum(len(block.get('original_text', '')) for block in original_blocks)
        translated_chars = sum(len(block.get('translated_text', '')) for block in translated_blocks)

        # 词数统计
        original_words = sum(len(block.get('original_text', '').split()) for block in original_blocks)
        translated_words = sum(len(block.get('translated_text', '').split()) for block in translated_blocks)

        # 计算变化率
        char_ratio = translated_chars / original_chars if original_chars > 0 else 1
        word_ratio = translated_words / original_words if original_words > 0 else 1

        return {
            'subtitle_count': len(original_blocks),
            'original_characters': original_chars,
            'translated_characters': translated_chars,
            'original_words': original_words,
            'translated_words': translated_words,
            'character_ratio': round(char_ratio, 2),
            'word_ratio': round(word_ratio, 2),
            'expansion_rate': round((char_ratio - 1) * 100, 1)  # 扩展率百分比
        }

    def _log_to_debug_file(self, filename_prefix: str, section: str, content: str, mode: str = 'a'):
        """
        记录调试信息到文件 (V17.0 默认使用追加模式)

        Args:
            filename_prefix (str): 文件名前缀
            section (str): 章节标题
            content (str): 内容
            mode (str): 文件写入模式，默认为追加 'a'
        """
        if not self.config.get('debug_mode', False):
            return

        stop_event = self.config.get("stop_event")
        if stop_event and stop_event.is_set():
            return

        try:
            debug_file = self._get_debug_file_path(filename_prefix)

            # [V17.0 修正] 明确使用传入的 mode
            with open(debug_file, mode, encoding='utf-8') as f:
                if section: # 只有在 section 非空时才写入标题
                    f.write(f"\n{section}\n")
                    f.write("=" * len(section) + "\n")
                f.write(content + "\n")
        except Exception as e:
            debug_print(f"记录调试信息失败: {str(e)}")

    def _get_debug_file_path(self, filename_prefix: str) -> Path:
        debug_dir = get_target_config_dir().parent / "debug_files"
        debug_filename = f"{filename_prefix}_api_interaction.txt"
        debug_file = debug_dir / debug_filename
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        return debug_file

    def _format_request_data(self, request_data: Dict[str, Any], headers: Dict[str, str],
                           provider: str, model: str) -> str:
        """
        格式化请求数据为可读字符串

        Args:
            request_data (Dict[str, Any]): 请求数据
            headers (Dict[str, str]): 请求头
            provider (str): API接口
            model (str): 模型名称

        Returns:
            str: 格式化后的请求数据
        """
        import json

        # 创建安全的请求头副本（隐藏API密钥）
        safe_headers = headers.copy()
        if 'authorization' in safe_headers:
            auth = safe_headers['authorization']
            if auth.startswith('Bearer '):
                safe_headers['authorization'] = f"Bearer {auth[7:10]}***{auth[-3:]}"
            else:
                safe_headers['authorization'] = f"***{auth[-3:]}"

        formatted_lines = [
            f"Provider: {provider}",
            f"Model: {model}",
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}",
            "",
            "Headers:",
            json.dumps(safe_headers, indent=2, ensure_ascii=False),
            "",
            "Request Body:",
            json.dumps(request_data, indent=2, ensure_ascii=False)
        ]

        return "\n".join(formatted_lines)

    @staticmethod
    def _format_translation_compare(inputs: List[str], outputs: List[str]) -> str:
        lines: List[str] = []
        total = max(len(inputs), len(outputs))
        for idx in range(total):
            source = inputs[idx] if idx < len(inputs) else ""
            target = outputs[idx] if idx < len(outputs) else ""
            lines.append(f"{idx + 1}. {source}")
            lines.append(f"   => {target}")
        return "\n".join(lines)

    def test_translation(self, test_text: str = "これはテストです。") -> str:
        """
        测试翻译功能

        Args:
            test_text (str): 测试文本

        Returns:
            str: 翻译结果
        """
        try:
            # 创建测试字幕块
            test_block = {
                'index': 1,
                'start_time': '00:00:01,000',
                'end_time': '00:00:03,000',
                'timestamp': '00:00:01,000 --> 00:00:03,000',
                'text': test_text,
                'original_text': test_text
            }

            # 翻译测试
            result = self.translate_batch([test_block])

            if result and len(result) > 0:
                return result[0].get('translated_text', '翻译失败')
            else:
                return '翻译失败：没有返回结果'

        except Exception as e:
            return f'翻译测试失败: {str(e)}'
