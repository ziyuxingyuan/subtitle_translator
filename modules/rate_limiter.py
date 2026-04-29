#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
速率限制器
负责管理API调用的速率限制，支持RPM和TPM控制
"""

import time
import threading
from collections import deque, defaultdict
from typing import Dict, Any, Optional, Callable
from datetime import datetime, timedelta


class RateLimiter:
    """API速率限制器"""

    def __init__(self):
        # 为每个接口维护独立的追踪数据
        self.provider_data = defaultdict(lambda: {
            'requests': deque(),  # 请求时间戳队列
            'tokens': deque(),    # token使用记录 (timestamp, token_count)
            'lock': threading.Lock()  # 线程锁
        })
        self.total_tokens = defaultdict(int)

        # 错误恢复时间缓存
        self.error_recovery_times = defaultdict(lambda: {
            'retry_after': 0,
            'error_time': 0
        })

    def _get_time_window(self, current_time: float) -> float:
        """获取时间窗口的起始时间（1分钟前）"""
        return current_time - 60.0  # 1分钟窗口

    def _clean_old_records(self, provider: str, current_time: float):
        """清理过期的记录"""
        data = self.provider_data[provider]
        window_start = self._get_time_window(current_time)

        with data['lock']:
            # 清理过期的请求记录
            while data['requests'] and data['requests'][0] < window_start:
                data['requests'].popleft()

            # 清理过期的token记录
            while data['tokens'] and data['tokens'][0][0] < window_start:
                data['tokens'].popleft()

    def _calculate_wait_time(self, provider: str, rate_limits: Dict[str, int],
                           estimated_tokens: int, current_time: float) -> float:
        """
        计算需要等待的时间

        Args:
            provider (str): API接口名称
            rate_limits (Dict[str, int]): 速率限制配置
            estimated_tokens (int): 预估token使用量
            current_time (float): 当前时间戳

        Returns:
            float: 需要等待的时间（秒）
        """
        data = self.provider_data[provider]
        window_start = self._get_time_window(current_time)

        max_rpm = rate_limits.get('requests_per_minute', 60)
        max_tpm = rate_limits.get('tokens_per_minute', 100000)

        wait_times = []

        with data['lock']:
            # 检查请求数限制
            current_requests = len(data['requests'])
            if max_rpm and max_rpm > 0 and current_requests >= max_rpm:
                # 找到最早的请求时间，计算需要等待多久
                oldest_request = data['requests'][0]
                wait_time = (oldest_request + 60.0) - current_time
                wait_times.append(max(0, wait_time))

            # 检查token限制
            current_tokens = sum(tokens for _, tokens in data['tokens'])
            if max_tpm and max_tpm > 0 and current_tokens + estimated_tokens > max_tpm:
                # 计算需要释放多少token才能满足本次请求
                tokens_needed = current_tokens + estimated_tokens - max_tpm
                tokens_to_remove = 0
                earliest_time = current_time

                # 从最早的token记录开始，计算需要移除多少token
                for timestamp, token_count in data['tokens']:
                    tokens_to_remove += token_count
                    earliest_time = timestamp
                    if tokens_to_remove >= tokens_needed:
                        break

                wait_time = (earliest_time + 60.0) - current_time
                wait_times.append(max(0, wait_time))

        # 返回最大的等待时间
        return max(wait_times) if wait_times else 0.0

    def _check_error_recovery(self, provider: str, current_time: float) -> float:
        """
        检查错误恢复时间

        Args:
            provider (str): API接口名称
            current_time (float): 当前时间戳

        Returns:
            float: 需要等待的时间（秒）
        """
        error_data = self.error_recovery_times[provider]

        if error_data['retry_after'] > 0:
            elapsed = current_time - error_data['error_time']
            if elapsed < error_data['retry_after']:
                return error_data['retry_after'] - elapsed
            else:
                # 重置错误恢复时间
                error_data['retry_after'] = 0
                error_data['error_time'] = 0

        return 0.0

    def wait_if_needed(self, provider: str, rate_limits: Dict[str, int],
                      estimated_tokens: int = 0,
                      stop_event: threading.Event | None = None) -> bool:
        """
        如果需要，等待直到可以发起请求

        Args:
            provider (str): API接口名称
            rate_limits (Dict[str, int]): 速率限制配置
            estimated_tokens (int): 预估token使用量

        Returns:
            bool: True表示正常等待，False表示遇到错误需要停止
        """
        current_time = time.time()

        # 首先检查错误恢复时间
        error_wait_time = self._check_error_recovery(provider, current_time)
        if error_wait_time > 0:
            print(f"检测到速率限制错误，等待 {error_wait_time:.1f} 秒后重试...")
            if stop_event:
                stop_event.wait(error_wait_time)
                if stop_event.is_set():
                    return False
            else:
                time.sleep(error_wait_time)
            return True

        # 清理过期记录
        self._clean_old_records(provider, current_time)

        # 计算需要等待的时间
        wait_time = self._calculate_wait_time(provider, rate_limits, estimated_tokens, current_time)

        if wait_time > 0:
            print(f"接近速率限制，等待 {wait_time:.1f} 秒...")
            if stop_event:
                stop_event.wait(wait_time)
                if stop_event.is_set():
                    return False
            else:
                time.sleep(wait_time)

        return True

    def reserve_request(self, provider: str, rate_limits: Dict[str, int],
                       estimated_tokens: int = 0,
                       stop_event: threading.Event | None = None,
                       log_func: Optional[Callable[[str], None]] = None) -> bool:
        """
        等待直到可以发起请求，并在成功时预占一次请求计数。

        Args:
            provider (str): API接口名称
            rate_limits (Dict[str, int]): 速率限制配置
            estimated_tokens (int): 预估token使用量
            stop_event (threading.Event | None): 停止事件
            log_func (Callable[[str], None] | None): 速率限制日志回调

        Returns:
            bool: True表示等待并占位成功，False表示被停止
        """
        while True:
            current_time = time.time()

            # 首先检查错误恢复时间
            error_wait_time = self._check_error_recovery(provider, current_time)
            if error_wait_time > 0:
                if not self._sleep_with_stop(error_wait_time, stop_event):
                    return False
                continue

            # 清理过期记录
            self._clean_old_records(provider, current_time)

            data = self.provider_data[provider]
            max_rpm = rate_limits.get('requests_per_minute', 60)
            max_tpm = rate_limits.get('tokens_per_minute', 100000)
            rpm_wait = 0.0
            tpm_wait = 0.0

            with data['lock']:
                current_requests = len(data['requests'])
                if max_rpm and max_rpm > 0 and current_requests >= max_rpm:
                    oldest_request = data['requests'][0]
                    rpm_wait = max(0, (oldest_request + 60.0) - current_time)

                current_tokens = sum(tokens for _, tokens in data['tokens'])
                if max_tpm and max_tpm > 0 and current_tokens + estimated_tokens > max_tpm:
                    tokens_needed = current_tokens + estimated_tokens - max_tpm
                    tokens_to_remove = 0
                    earliest_time = current_time
                    for timestamp, token_count in data['tokens']:
                        tokens_to_remove += token_count
                        earliest_time = timestamp
                        if tokens_to_remove >= tokens_needed:
                            break
                    tpm_wait = max(0, (earliest_time + 60.0) - current_time)

                wait_time = max(rpm_wait, tpm_wait)
                if wait_time <= 0:
                    data['requests'].append(current_time)
                    return True

            if rpm_wait > 0 and rpm_wait >= tpm_wait and log_func:
                log_func(f"触发RPM限制，等待 {wait_time:.1f} 秒...")

            if not self._sleep_with_stop(wait_time, stop_event):
                return False

    def record_tokens(self, provider: str, input_tokens: int = 0,
                      output_tokens: int = 0) -> None:
        """
        记录token使用量（不记录请求次数）。

        Args:
            provider (str): API接口名称
            input_tokens (int): 输入token数量
            output_tokens (int): 输出token数量
        """
        total_tokens = input_tokens + output_tokens
        if total_tokens <= 0:
            return

        current_time = time.time()
        data = self.provider_data[provider]
        with data['lock']:
            data['tokens'].append((current_time, total_tokens))
            self.total_tokens[provider] += total_tokens

    def record_request(self, provider: str, input_tokens: int = 0,
                      output_tokens: int = 0):
        """
        记录一次API请求

        Args:
            provider (str): API接口名称
            input_tokens (int): 输入token数量
            output_tokens (int): 输出token数量
        """
        current_time = time.time()
        data = self.provider_data[provider]
        total_tokens = input_tokens + output_tokens

        with data['lock']:
            # 记录请求时间
            data['requests'].append(current_time)

            # 记录token使用
            if total_tokens > 0:
                data['tokens'].append((current_time, total_tokens))
                self.total_tokens[provider] += total_tokens

    @staticmethod
    def _sleep_with_stop(seconds: float, stop_event: threading.Event | None) -> bool:
        if seconds <= 0:
            return True
        if stop_event:
            stop_event.wait(seconds)
            return not stop_event.is_set()
        time.sleep(seconds)
        return True

    def record_rate_limit_error(self, provider: str, error_message: str,
                              retry_after: Optional[int] = None):
        """
        记录速率限制错误

        Args:
            provider (str): API接口名称
            error_message (str): 错误信息
            retry_after (Optional[int]): 重试等待时间（秒）
        """
        current_time = time.time()
        error_data = self.error_recovery_times[provider]

        # 解析错误信息中的等待时间
        if retry_after is None:
            retry_after = self._parse_retry_after(error_message)

        if retry_after > 0:
            # 设置最大等待时间为5分钟（300秒），避免过长时间等待
            max_wait_time = 300
            if retry_after > max_wait_time:
                retry_after = max_wait_time
                print(f"等待时间过长，已限制为最大等待时间: {max_wait_time}秒")

            error_data['retry_after'] = retry_after
            error_data['error_time'] = current_time
            print(f"记录速率限制错误: {provider}, 等待时间: {retry_after}秒")

    def _parse_retry_after(self, error_message: str) -> int:
        """
        从错误信息中解析重试等待时间

        Args:
            error_message (str): 错误信息

        Returns:
            int: 等待时间（秒）
        """
        import re

        # 查找 "try again in XhYmZs.Ss" 或 "Please try again in XhYmZs.Ss" 格式
        pattern = r'please try again in (\d+)h(\d+)m(\d+(?:\.\d+)?)s'
        match = re.search(pattern, error_message.lower())
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))

        # 再尝试不带 "please" 的格式
        pattern = r'try again in (\d+)h(\d+)m(\d+(?:\.\d+)?)s'
        match = re.search(pattern, error_message.lower())
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))

        # 查找 "Please try again in X seconds" 格式
        pattern = r'(\d+(?:\.\d+)?)\s*seconds?'
        match = re.search(pattern, error_message.lower())
        if match:
            return int(float(match.group(1)))

        # 查找 "wait X seconds" 格式
        pattern = r'wait\s+(\d+(?:\.\d+)?)\s*seconds?'
        match = re.search(pattern, error_message.lower())
        if match:
            return int(float(match.group(1)))

        # 查找 "分钟内最多请求X次" 类型错误，设置默认等待60秒
        if '分钟内最多请求' in error_message or 'requests per minute' in error_message.lower():
            return 60

        # 查找TPM错误，设置默认等待直到下一分钟
        if 'tokens per min' in error_message.lower() or 'tpm' in error_message.lower():
            return 60

        # 默认等待30秒
        return 30

    def get_current_usage(self, provider: str) -> Dict[str, Any]:
        """
        获取当前使用情况

        Args:
            provider (str): API接口名称

        Returns:
            Dict[str, Any]: 当前使用情况
        """
        current_time = time.time()
        self._clean_old_records(provider, current_time)

        data = self.provider_data[provider]

        with data['lock']:
            current_requests = len(data['requests'])
            current_tokens = sum(tokens for _, tokens in data['tokens'])

            return {
                'current_requests': current_requests,
                'current_tokens': current_tokens,
                'window_start': self._get_time_window(current_time),
                'current_time': current_time
            }

    def get_total_tokens(self, provider: str) -> int:
        """
        获取累计token使用量（不按时间窗口清理）。
        """
        data = self.provider_data[provider]
        with data['lock']:
            return int(self.total_tokens.get(provider, 0))

    def reset_provider(self, provider: str):
        """
        重置指定接口的统计数据

        Args:
            provider (str): API接口名称
        """
        data = self.provider_data[provider]
        with data['lock']:
            data['requests'].clear()
            data['tokens'].clear()
            self.total_tokens[provider] = 0

        error_data = self.error_recovery_times[provider]
        error_data['retry_after'] = 0
        error_data['error_time'] = 0
