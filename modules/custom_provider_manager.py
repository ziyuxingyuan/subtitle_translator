#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自定义API接口管理器
负责管理用户自定义的API接口配置
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
import re

from modules.config_paths import get_config_dir


class CustomProviderManager:
    """自定义API接口管理器"""

    def __init__(self, config_file: str | None = None):
        config_path = Path(config_file) if config_file else (get_config_dir() / "custom_providers.json")
        self.config_file = config_path
        self.providers = self._load_providers()

    def _load_providers(self) -> Dict[str, Any]:
        """加载自定义接口配置"""
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data.get("custom_providers", {})
            else:
                # 创建默认配置文件
                default_config = {
                    "custom_providers": {},
                    "metadata": {
                        "version": "1.0",
                        "created_at": datetime.now().isoformat(),
                        "description": "自定义API接口配置文件"
                    }
                }
                self._save_config(default_config)
                return {}
        except Exception as e:
            print(f"加载自定义接口配置失败: {str(e)}")
            return {}

    def _save_config(self, data: Dict[str, Any]) -> bool:
        """保存配置到文件"""
        try:
            # 确保目录存在
            self.config_file.parent.mkdir(parents=True, exist_ok=True)

            # 更新元数据
            if "metadata" not in data:
                data["metadata"] = {}
            data["metadata"]["updated_at"] = datetime.now().isoformat()

            # 保存文件
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存自定义接口配置失败: {str(e)}")
            return False

    def add_provider(self, name: str, config: Dict[str, Any]) -> Tuple[bool, str]:
        """
        添加自定义接口

        Args:
            name (str): 接口名称
            config (Dict[str, Any]): 接口配置

        Returns:
            Tuple[bool, str]: (是否成功, 消息)
        """
        try:
            # 验证配置
            is_valid, message = self._validate_config(config)
            if not is_valid:
                return False, f"配置验证失败: {message}"

            # 检查名称是否已存在
            if name in self.providers:
                return False, f"接口名称 '{name}' 已存在"

            # 生成唯一ID
            provider_id = str(uuid.uuid4())

            # 添加元数据
            config_with_metadata = {
                **config,
                "id": provider_id,
                "name": name,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "is_custom": True
            }

            # 保存配置
            self.providers[name] = config_with_metadata

            # 更新文件
            data = {
                "custom_providers": self.providers,
                "metadata": {
                    "version": "1.0",
                    "updated_at": datetime.now().isoformat(),
                    "total_providers": len(self.providers)
                }
            }

            if self._save_config(data):
                return True, f"自定义接口 '{name}' 添加成功"
            else:
                # 如果保存失败，回滚内存中的更改
                del self.providers[name]
                return False, "保存配置失败"

        except Exception as e:
            return False, f"添加自定���接口失败: {str(e)}"

    def update_provider(self, name: str, config: Dict[str, Any]) -> Tuple[bool, str]:
        """
        更新自定义接口

        Args:
            name (str): 接口名称
            config (Dict[str, Any]): 新的配置

        Returns:
            Tuple[bool, str]: (是否成功, 消息)
        """
        try:
            # 检查接口是否存在
            if name not in self.providers:
                return False, f"自定义接口 '{name}' 不存在"

            # 验证配置
            is_valid, message = self._validate_config(config)
            if not is_valid:
                return False, f"配置验证失败: {message}"

            # 保留原有的元数据
            existing_config = self.providers[name].copy()
            config_with_metadata = {
                **existing_config,
                **config,
                "updated_at": datetime.now().isoformat()
            }

            # 更新配置
            self.providers[name] = config_with_metadata

            # 保存到文件
            data = {
                "custom_providers": self.providers,
                "metadata": {
                    "version": "1.0",
                    "updated_at": datetime.now().isoformat(),
                    "total_providers": len(self.providers)
                }
            }

            if self._save_config(data):
                return True, f"自定义接口 '{name}' 更新成功"
            else:
                return False, "保存配置失败"

        except Exception as e:
            return False, f"更新自定义接口失败: {str(e)}"

    def delete_provider(self, name: str) -> Tuple[bool, str]:
        """
        删除自定义接口

        Args:
            name (str): 接口名称

        Returns:
            Tuple[bool, str]: (是否成功, 消息)
        """
        try:
            if name not in self.providers:
                return False, f"自定义接口 '{name}' 不存在"

            # 删除配置
            del self.providers[name]

            # 保存到文件
            data = {
                "custom_providers": self.providers,
                "metadata": {
                    "version": "1.0",
                    "updated_at": datetime.now().isoformat(),
                    "total_providers": len(self.providers)
                }
            }

            if self._save_config(data):
                return True, f"自定义接口 '{name}' 删除成功"
            else:
                # 如果保存失败，回滚内存中的更改
                # 这里无法完美回滚，因为配置已经被删除了
                return False, "保存配置失败"

        except Exception as e:
            return False, f"删除自定义接口失败: {str(e)}"

    def rename_provider(self, old_name: str, new_name: str) -> Tuple[bool, str]:
        """
        重命名自定义接口

        Args:
            old_name (str): 旧的接口名称
            new_name (str): 新的接口名称

        Returns:
            Tuple[bool, str]: (是否成功, 消息)
        """
        try:
            # 基础验证
            if not old_name or not new_name:
                return False, "新旧名称都不能为空"
            if old_name == new_name:
                return True, "名称未改变"  # 名称相同，无需操作
            if old_name not in self.providers:
                return False, f"原始接口 '{old_name}' 不存在"
            if new_name in self.providers:
                return False, f"目标名称 '{new_name}' 已被占用"

            # 执行重命名
            provider_config = self.providers.pop(old_name)  # 弹出旧的
            provider_config['name'] = new_name  # 更新配置内部的名称字段
            provider_config['updated_at'] = datetime.now().isoformat()
            self.providers[new_name] = provider_config  # 插入新的

            # 构造要保存的完整数据
            data = {
                "custom_providers": self.providers,
                "metadata": {
                    "version": "1.0",
                    "updated_at": datetime.now().isoformat(),
                    "total_providers": len(self.providers)
                }
            }

            if self._save_config(data):
                return True, f"接口已成功从 '{old_name}' 重命名为 '{new_name}'"
            else:
                # 紧急回滚
                self.providers[old_name] = self.providers.pop(new_name)
                return False, "重命名后保存配置失败，操作已回滚"

        except Exception as e:
            return False, f"重命名接口时出错: {str(e)}"

    def get_provider(self, name: str) -> Optional[Dict[str, Any]]:
        """获取指定自定义接口的配置"""
        return self.providers.get(name)

    def get_all_providers(self) -> Dict[str, Dict[str, Any]]:
        """获取所有自定义接口"""
        return self.providers.copy()

    def get_provider_names(self) -> List[str]:
        """获取所有自定义接口名称"""
        return list(self.providers.keys())

    def provider_exists(self, name: str) -> bool:
        """检查接口是否存在"""
        return name in self.providers

    def _validate_config(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        """
        验证接口配置

        Args:
            config (Dict[str, Any]): 配置信息

        Returns:
            Tuple[bool, str]: (是否有效, 错误消息)
        """
        try:
            # 检查必需字段
            required_fields = ["base_url", "models", "default_model", "auth_type"]
            for field in required_fields:
                if field not in config:
                    return False, f"缺少必需字段: {field}"

            # 验证base_url
            base_url = config["base_url"].strip()
            if not base_url:
                return False, "API地址不能为空"

            # 简单的URL格式验证
            url_pattern = re.compile(
                r'^https?://'  # http:// or https://
                r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
                r'localhost|'  # localhost...
                r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
                r'(?::\d+)?'  # optional port
                r'(?:/?|[/?]\S+)$', re.IGNORECASE)

            if not url_pattern.match(base_url):
                return False, "API地址格式不正确，请输入有效的URL"

            # 验证模型列表
            models = config["models"]
            if not isinstance(models, list) or not models:
                return False, "模型列表不能为空"

            # 验证默认模型
            default_model = config["default_model"]
            if default_model not in models:
                return False, f"默认模型 '{default_model}' 不在模型列表中"

            # 验证认证类型
            auth_type = config["auth_type"]
            if auth_type not in ["bearer", "api_key", "custom"]:
                return False, "认证类型必须是 bearer、api_key 或 custom"

            # 如果是自定义认证，需要更多验证
            if auth_type == "custom":
                if "api_key_header" not in config:
                    return False, "自定义认证需要指定 api_key_header"
                if "api_key_format" not in config:
                    return False, "自定义认证需要指定 api_key_format"

            return True, "配置验证通过"

        except Exception as e:
            return False, f"配置验证时出错: {str(e)}"

    def export_providers(self, file_path: str) -> Tuple[bool, str]:
        """
        导出自定义接口配置

        Args:
            file_path (str): 导出文件路径

        Returns:
            Tuple[bool, str]: (是否成功, 消息)
        """
        try:
            export_data = {
                "exported_at": datetime.now().isoformat(),
                "version": "1.0",
                "custom_providers": self.providers
            }

            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)

            return True, f"成功导出 {len(self.providers)} 个自定义接口到 {file_path}"
        except Exception as e:
            return False, f"导出配置失败: {str(e)}"

    def import_providers(self, file_path: str, merge: bool = True) -> Tuple[bool, str]:
        """
        导入自定义接口配置

        Args:
            file_path (str): 导入文件路径
            merge (bool): 是否与现有配置合并，False则覆盖

        Returns:
            Tuple[bool, str]: (是否成功, 消息)
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                import_data = json.load(f)

            imported_providers = import_data.get("custom_providers", {})
            if not imported_providers:
                return False, "导入文件中没有找到自定义接口配置"

            if not merge:
                # 覆盖模式
                self.providers = imported_providers
            else:
                # 合并模式
                conflict_count = 0
                for name, config in imported_providers.items():
                    if name in self.providers:
                        conflict_count += 1
                        # 重命名冲突的接口
                        new_name = f"{name}_imported_{int(datetime.now().timestamp())}"
                        self.providers[new_name] = config
                    else:
                        self.providers[name] = config

                if conflict_count > 0:
                    print(f"警告: 发现 {conflict_count} 个名称冲突，已重命名导入的接口")

            # 保存配置
            data = {
                "custom_providers": self.providers,
                "metadata": {
                    "version": "1.0",
                    "updated_at": datetime.now().isoformat(),
                    "total_providers": len(self.providers),
                    "imported_from": file_path
                }
            }

            if self._save_config(data):
                action = "合并" if merge else "覆盖"
                return True, f"成功{action}导入 {len(imported_providers)} 个自定义接口"
            else:
                return False, "保存导入配置失败"

        except Exception as e:
            return False, f"导入配置失败: {str(e)}"

    def get_provider_info(self, name: str) -> Optional[Dict[str, Any]]:
        """获取自定义接口信息（兼容APIManager接口）"""
        provider = self.get_provider(name)
        if provider:
            return {
                "name": provider.get("name", name),
                "description": provider.get("description", "自定义API接口"),
                "base_url": provider.get("base_url", ""),
                "models": provider.get("models", []),
                "default_model": provider.get("default_model", ""),
                "auth_type": provider.get("auth_type", "bearer"),
                "api_key_header": provider.get("api_key_header", "Authorization"),
                "api_key_format": provider.get("api_key_format", "Bearer {key}"),
                "max_tokens": provider.get("max_tokens", 4096),
                "requests_per_minute": provider.get("requests_per_minute", 0),
                "tokens_per_minute": provider.get("tokens_per_minute", 0),
                "supports_stream": provider.get("supports_stream", False),
                "rate_limit": provider.get("rate_limit", {
                    "requests_per_minute": 60,
                    "tokens_per_minute": 100000
                }),
                "is_custom": True
            }
        return None

    def search_providers(self, keyword: str) -> List[str]:
        """
        搜索自定义接口

        Args:
            keyword (str): 搜索关键词

        Returns:
            List[str]: 匹配的接口名称列表
        """
        keyword = keyword.lower()
        matches = []

        for name, config in self.providers.items():
            # 搜索名称
            if keyword in name.lower():
                matches.append(name)
                continue

            # 搜索描述
            description = config.get("description", "").lower()
            if keyword in description:
                matches.append(name)
                continue

            # 搜索base_url
            base_url = config.get("base_url", "").lower()
            if keyword in base_url:
                matches.append(name)

        return matches

    def get_statistics(self) -> Dict[str, Any]:
        """获取自定义接口统计信息"""
        stats = {
            "total_providers": len(self.providers),
            "total_models": 0,
            "auth_types": {},
            "avg_models_per_provider": 0
        }

        if self.providers:
            model_counts = []
            for config in self.providers.values():
                models = config.get("models", [])
                model_counts.append(len(models))
                stats["total_models"] += len(models)

                # 统计认证类型
                auth_type = config.get("auth_type", "bearer")
                stats["auth_types"][auth_type] = stats["auth_types"].get(auth_type, 0) + 1

            stats["avg_models_per_provider"] = sum(model_counts) / len(model_counts)

        return stats
