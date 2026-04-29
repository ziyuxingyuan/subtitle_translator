#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API管理器
负责管理不同的AI API接口
"""

import json
import requests
from pathlib import Path
from typing import Dict, List, Any, Optional
from .custom_provider_manager import CustomProviderManager
from .config_paths import get_config_dir


class APIManager:
    """API管理器"""

    def __init__(self, config_file: str | None = None):
        config_path = Path(config_file) if config_file else (get_config_dir() / "api_providers.json")
        self.config_file = config_path
        self.providers_config = self._load_providers_config()
        self.custom_manager = CustomProviderManager()

    # --- [V25.0 核心新增] ---
    def reload_providers(self):
        """
        强制从文件重新加载所有接口配置。
        这是解决UI不同步问题的关键。
        """
        print("APIManager: 强制重新加载所有接口配置...")
        # 重新加载自定义接口管理器
        self.custom_manager = CustomProviderManager()
        # 重新加载所有接口配置（包括内置和自定义）
        self.providers_config = self._load_providers_config()
        print("APIManager: 接口配置重新加载完成")

    def _load_providers_config(self) -> Dict[str, Any]:
        """加载API接口配置"""
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                return config
            else:
                print(f"API接口配置文件不存在: {self.config_file}")
                return {}
        except Exception as e:
            print(f"加载API接口配置失败: {str(e)}")
            return {}

    def get_providers(self) -> List[str]:
        """获取所有可用的API接口列表"""
        builtin_providers = list(self.providers_config.get("providers", {}).keys())
        custom_providers = self.custom_manager.get_provider_names()
        return builtin_providers + custom_providers

    def get_provider_info(self, provider: str) -> Optional[Dict[str, Any]]:
        """获取指定API接口的信息"""
        # 首先查找内置接口
        providers = self.providers_config.get("providers", {})
        if provider in providers:
            return providers[provider]

        # 然后查找自定义接口
        return self.custom_manager.get_provider_info(provider)

    def get_available_models(self, provider: str) -> List[str]:
        """获取指定API接口的可用模型列表"""
        provider_info = self.get_provider_info(provider)
        if provider_info:
            return provider_info.get("models", [])
        return []

    def get_default_model(self, provider: str) -> str:
        """获取指定API接口的默认模型"""
        provider_info = self.get_provider_info(provider)
        if provider_info:
            return provider_info.get("default_model", "")
        return ""

    def get_default_endpoint(self, provider: str) -> str:
        """获取指定API接口的默认地址"""
        provider_info = self.get_provider_info(provider)
        if provider_info:
            return provider_info.get("base_url", "")
        return ""

    def get_auth_type(self, provider: str) -> str:
        """获取指定API接口的认证类型"""
        provider_info = self.get_provider_info(provider)
        if provider_info:
            return provider_info.get("auth_type", "bearer")
        return "bearer"

    def get_auth_headers(self, provider: str, api_key: str) -> Dict[str, str]:
        """获取认证头信息"""
        provider_info = self.get_provider_info(provider)
        if not provider_info:
            return {"Authorization": f"Bearer {api_key}"}

        auth_type = provider_info.get("auth_type", "bearer")
        header_name = provider_info.get("api_key_header", "Authorization")
        key_format = provider_info.get("api_key_format", "Bearer {key}")

        if auth_type == "bearer":
            return {header_name: key_format.format(key=api_key)}
        elif auth_type == "api_key":
            return {header_name: api_key}
        else:
            return {"Authorization": f"Bearer {api_key}"}

    def get_provider_limits(self, provider: str) -> Dict[str, int]:
        """
        获取指定接口的速率限制 (RPM 和 TPM)。
        返回一个包含 'requests_per_minute' 和 'tokens_per_minute' 的字典。
        如果未设置，则值为 0 (代表无限制)。
        """
        provider_info = self.get_provider_info(provider)
        if not provider_info:
            return {"requests_per_minute": 0, "tokens_per_minute": 0}

        # 优先使用新的直接字段格式
        requests_per_minute = provider_info.get("requests_per_minute")
        tokens_per_minute = provider_info.get("tokens_per_minute")

        # 如果新格式不存在，尝试从旧的rate_limit对象获取（向后兼容）
        if requests_per_minute is None or tokens_per_minute is None:
            rate_limit = provider_info.get("rate_limit", {})
            requests_per_minute = requests_per_minute or rate_limit.get("requests_per_minute", 0)
            tokens_per_minute = tokens_per_minute or rate_limit.get("tokens_per_minute", 0)

        return {
            "requests_per_minute": requests_per_minute,
            "tokens_per_minute": tokens_per_minute
        }

    def get_rate_limit(self, provider: str) -> Dict[str, int]:
        """获取API调用频率限制（保持向后兼容）"""
        return self.get_provider_limits(provider)

    def get_max_tokens(self, provider: str) -> int:
        """获取最大token限制"""
        provider_info = self.get_provider_info(provider)
        if provider_info:
            return provider_info.get("max_tokens", 4096)
        return 4096

    def get_config_templates(self) -> Dict[str, Dict[str, str]]:
        """获取配置模板"""
        return self.providers_config.get("templates", {})

    def validate_provider(self, provider: str) -> bool:
        """验证API接口是否有效"""
        return provider in self.get_providers()

    def provider_exists(self, provider: str) -> bool:
        """检查API接口是否存在（兼容自定义接口管理器的接口）"""
        return self.validate_provider(provider)

    def validate_model(self, provider: str, model: str) -> bool:
        """验证模型是否有效"""
        available_models = self.get_available_models(provider)
        return model in available_models

    def test_api_connection(self, provider: str, api_key: str,
                          endpoint: str = None, model: str = None) -> tuple[bool, str]:
        """
        测试API连接

        Args:
            provider (str): API接口
            api_key (str): API密钥
            endpoint (str): API地址
            model (str): 模型名称

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        try:
            if not self.validate_provider(provider):
                return False, f"无效的API接口: {provider}"

            # 获取配置
            provider_info = self.get_provider_info(provider)
            if not provider_info:
                return False, f"找不到接口配置: {provider}"

            # 使用默认地址
            if not endpoint:
                endpoint = provider_info.get("base_url", "")
            if not endpoint:
                return False, "API地址为空"

            # 使用默认模型
            if not model:
                model = provider_info.get("default_model", "")
            if not model:
                return False, "模型名称为空"

            # 构建测试请求
            headers = self.get_auth_headers(provider, api_key)
            headers["Content-Type"] = "application/json"

            # 测试消息
            test_data = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Hello, this is a test message. Please respond with 'OK'."
                    }
                ],
                "max_tokens": 10,
                "temperature": 0.1
            }

            # 发送请求
            chat_endpoint = f"{endpoint.rstrip('/')}/chat/completions"
            response = requests.post(chat_endpoint,
                                   json=test_data,
                                   headers=headers,
                                   timeout=30)

            if response.status_code == 200:
                return True, "API连接测试成功"
            else:
                error_msg = f"API请求失败 (HTTP {response.status_code})"
                try:
                    error_detail = response.json()
                    if "error" in error_detail:
                        error_msg += f": {error_detail['error'].get('message', '未知错误')}"
                except:
                    if response.text:
                        error_msg += f": {response.text[:200]}"
                return False, error_msg

        except requests.exceptions.Timeout:
            return False, "API请求超时，请检查网络连接"
        except requests.exceptions.ConnectionError:
            return False, "无法连接到API服务器，请检查网络和地址地址"
        except Exception as e:
            return False, f"测试API连接时出错: {str(e)}"

    def get_provider_description(self, provider: str) -> str:
        """获取API接口描述"""
        provider_info = self.get_provider_info(provider)
        if provider_info:
            return provider_info.get("description", "")
        return ""

    def supports_streaming(self, provider: str) -> bool:
        """检查是否支持流式响应"""
        provider_info = self.get_provider_info(provider)
        if provider_info:
            return provider_info.get("supports_stream", False)
        return False

    def get_recommended_settings(self, provider: str) -> Dict[str, Any]:
        """获取推荐的设置"""
        provider_info = self.get_provider_info(provider)
        if not provider_info:
            return {}

        # 使用新的速率限制获取方法
        rate_limits = self.get_provider_limits(provider)
        max_tokens = provider_info.get("max_tokens", 4096)

        # 根据频率限制推荐批次大小
        requests_per_minute = rate_limits.get("requests_per_minute", 60)
        if requests_per_minute >= 100:
            recommended_batch_size = 50
        elif requests_per_minute >= 60:
            recommended_batch_size = 30
        else:
            recommended_batch_size = 20

        # 直接使用接口配置的max_tokens设置，或使用智能推荐
        if max_tokens >= 32768:  # 对于大模型，可以使用较���值
            recommended_max_tokens = min(max_tokens // 2, 16384)  # 不超过16k
        elif max_tokens >= 8000:
            recommended_max_tokens = min(max_tokens // 2, 8000)   # 不超过8k
        elif max_tokens >= 4000:
            recommended_max_tokens = min(max_tokens // 2, 4000)   # 不超过4k
        else:
            recommended_max_tokens = max_tokens // 2

        return {
            "batch_size": recommended_batch_size,
            "max_tokens": recommended_max_tokens,
            "temperature": 0.2,
            "timeout": 30
        }

    def estimate_cost(self, provider: str, input_tokens: int, output_tokens: int) -> Dict[str, float]:
        """
        估算API调用成本
        注意：这里的价格是示例值，实际价格需要根据最新的API定价更新

        Args:
            provider (str): API接口
            input_tokens (int): 输入token数量
            output_tokens (int): 输出token数量

        Returns:
            Dict[str, float]: 成本估算信息
        """
        # 这里使用示例价格，实际使用时需要更新为真实价格
        price_info = {
            "openai": {
                "gpt-4": {"input": 0.03, "output": 0.06},  # per 1K tokens
                "gpt-3.5-turbo": {"input": 0.001, "output": 0.002}
            },
            "claude": {
                "claude-3-5-sonnet-20241022": {"input": 0.003, "output": 0.015}
            },
            "qwen": {
                "qwen-plus": {"input": 0.0008, "output": 0.002}
            }
        }

        try:
            provider_prices = price_info.get(provider, {})
            # 这里简化处理，使用平均价格
            avg_input_price = 0.001  # $1 per 1M tokens
            avg_output_price = 0.002  # $2 per 1M tokens

            input_cost = (input_tokens / 1000) * avg_input_price
            output_cost = (output_tokens / 1000) * avg_output_price
            total_cost = input_cost + output_cost

            return {
                "input_cost": round(input_cost, 6),
                "output_cost": round(output_cost, 6),
                "total_cost": round(total_cost, 6),
                "currency": "USD"
            }
        except Exception as e:
            print(f"估算成本失败: {str(e)}")
            return {
                "input_cost": 0.0,
                "output_cost": 0.0,
                "total_cost": 0.0,
                "currency": "USD"
            }

    def get_error_suggestions(self, provider: str, error_code: int, error_message: str) -> List[str]:
        """
        根据错误类型提供解决建议

        Args:
            provider (str): API接口
            error_code (int): HTTP状态码
            error_message (str): 错误消息

        Returns:
            List[str]: 解决建议列表
        """
        suggestions = []

        # 通用错误处理
        if error_code == 401:
            suggestions.extend([
                "检查API密钥是否正确",
                "确认API密钥是否已激活",
                "检查API密钥是否已过期"
            ])
        elif error_code == 403:
            suggestions.extend([
                "检查API密钥是否有访问权限",
                "确认账户余额是否充足",
                "检查是否已开通相应服务的权限"
            ])
        elif error_code == 429:
            suggestions.extend([
                "降低请求频率",
                "增加批次之间的间隔时间",
                "检查是否超过了API调用限制"
            ])
        elif error_code == 500:
            suggestions.extend([
                "稍后重试",
                "检查API服务状态",
                "尝试更换不同的地址地址"
            ])

        # 特定接口的错误处理
        if provider == "openai":
            if "insufficient quota" in error_message.lower():
                suggestions.append("检查OpenAI账户余额")
            elif "rate limit" in error_message.lower():
                suggestions.append("降低每分钟请求数量")
        elif provider == "claude":
            if "rate limit" in error_message.lower():
                suggestions.append("检查Claude API使用限制")
        elif provider == "qwen":
            if "quota" in error_message.lower():
                suggestions.append("检查阿里云账户余额")

        # 添加通用建议
        if not suggestions:
            suggestions.extend([
                "检查网络连接",
                "确认API地址地址正确",
                "查看详细错误日志"
            ])

        return suggestions

    def get_provider_model_mapping(self) -> Dict[str, List[str]]:
        """
        获取所有接口及其可用模型的映射关系
        返回格式: {provider_name: [model1, model2, ...]}
        """
        mapping = {}
        all_providers = self.get_providers()

        for provider in all_providers:
            models = self.get_available_models(provider)
            mapping[provider] = models

        return mapping

    def get_models_for_provider(self, provider: str) -> List[str]:
        """
        获取指定接口的可用模型列表
        Args:
            provider (str): 接口名称
        Returns:
            List[str]: 模型列表
        """
        return self.get_available_models(provider)

    def cache_api_key(self, provider: str, api_key: str) -> None:
        """
        缓存API密钥（用于避免重复输入）
        Args:
            provider (str): 接口名称
            api_key (str): API密钥
        """
        # 这里可以实现密钥缓存逻辑
        # 为了安全起见，实际实现中应该加密存储
        pass

    def get_cached_api_key(self, provider: str) -> Optional[str]:
        """
        获取缓存的API密钥
        Args:
            provider (str): 接口名称
        Returns:
            Optional[str]: 缓存的API密钥，如果不存在则返回None
        """
        # 这里可以实现密钥获取逻辑
        return None

    def validate_and_cache_api_key(self, provider: str, api_key: str) -> bool:
        """
        验证并缓存API密钥
        Args:
            provider (str): 接口名称
            api_key (str): API密钥
        Returns:
            bool: 验证是否成功
        """
        # 这里可以实现密钥验证和缓存逻辑
        return True

    def add_custom_provider(self, name: str, config: Dict[str, Any]) -> tuple[bool, str]:
        """
        添加自定义API接口

        Args:
            name (str): 接口名称
            config (Dict[str, Any]): 接口配置

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        return self.custom_manager.add_provider(name, config)

    def update_custom_provider(self, name: str, config: Dict[str, Any]) -> tuple[bool, str]:
        """
        更新自定义API接口

        Args:
            name (str): 接口名称
            config (Dict[str, Any]): 新的配置

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        return self.custom_manager.update_provider(name, config)

    def delete_custom_provider(self, name: str) -> tuple[bool, str]:
        """
        删除自定义API接口

        Args:
            name (str): 接口名称

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        return self.custom_manager.delete_provider(name)

    def rename_custom_provider(self, old_name: str, new_name: str) -> tuple[bool, str]:
        """
        重命名自定义API接口

        Args:
            old_name (str): 旧的接口名称
            new_name (str): 新的接口名称

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        return self.custom_manager.rename_provider(old_name, new_name)

    def get_custom_providers(self) -> Dict[str, Dict[str, Any]]:
        """获取所有自定义接口"""
        return self.custom_manager.get_all_providers()

    def is_custom_provider(self, provider: str) -> bool:
        """检查是否为自定义接口"""
        return self.custom_manager.provider_exists(provider)

    def export_custom_providers(self, file_path: str) -> tuple[bool, str]:
        """
        导出自定义接口配置

        Args:
            file_path (str): 导出文件路径

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        return self.custom_manager.export_providers(file_path)

    def import_custom_providers(self, file_path: str, merge: bool = True) -> tuple[bool, str]:
        """
        导入自定义接口配置

        Args:
            file_path (str): 导入文件路径
            merge (bool): 是否与现有配置合并

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        return self.custom_manager.import_providers(file_path, merge)

    def search_custom_providers(self, keyword: str) -> List[str]:
        """
        搜索自定义接口

        Args:
            keyword (str): 搜索关键词

        Returns:
            List[str]: 匹配的接口名称列表
        """
        return self.custom_manager.search_providers(keyword)

    def get_custom_provider_statistics(self) -> Dict[str, Any]:
        """获取自定义接口统计信息"""
        return self.custom_manager.get_statistics()

    def test_custom_provider_connection(self, name: str, api_key: str) -> tuple[bool, str]:
        """
        测试自定义API接口连接

        Args:
            name (str): 自定义接口名称
            api_key (str): API密钥

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        if not self.custom_manager.provider_exists(name):
            return False, f"自定义接口 '{name}' 不存在"

        provider_info = self.custom_manager.get_provider_info(name)
        if not provider_info:
            return False, f"无法获取接口 '{name}' 的配置信息"

        return self.test_api_connection(
            provider=name,
            api_key=api_key,
            endpoint=provider_info.get("base_url", ""),
            model=provider_info.get("default_model", "")
        )

    def is_streaming_enabled_for_provider(self, provider: str) -> bool:
        """
        检查指定的接口是否在配置中启用了流式传输。
        这通常用于自定义接口的用户设置。

        Args:
            provider (str): 接口名称

        Returns:
            bool: 如果配置中 "supports_stream" 为 True，则返回 True。
        """
        provider_info = self.get_provider_info(provider)
        if provider_info:
            # .get('supports_stream', False) 确保如果没这个键，则默认为False
            # 同时兼容旧的 'use_streaming' 字段名
            return provider_info.get("supports_stream", provider_info.get("use_streaming", False))
        return False
