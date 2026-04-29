#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理器
负责用户配置的保存、加载和管理
"""

import json
import os
from pathlib import Path
from typing import Dict, Any, Optional

from modules.config_paths import ensure_config_dir, get_config_dir


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_dir: str | None = None):
        resolved_dir = Path(config_dir) if config_dir else get_config_dir()
        self.config_dir = ensure_config_dir(resolved_dir)

        self.user_config_file = self.config_dir / "user_settings.json"
        self.api_providers_file = self.config_dir / "api_providers.json"
        self.system_prompts_file = self.config_dir / "system_prompts.json"

        # 默认配置
        self.default_config = {
            "current_provider": "openai",
            "api_keys": {},  # 每个接口独立的API密钥
            "model": "gpt-4",
            "endpoint": "https://api.openai.com/v1",
            "source_language": "日文",
            "target_language": "中文",
            "batch_size": 100,
            "max_retries": 3,
            "batch_retries": 2,
            "concurrency": 0,  # 并发任务数，0为自动模式
            "timeout": 240,    # 超时时间（秒）
            "batch_timeout": 0,  # 批次硬超时（秒），0表示关闭
            "proxy_address": "",  # 代理服务器地址
            "proxy_enabled": False,  # 是否启用代理
            "debug_mode": False,
            "window_geometry": "1200x800",
            "last_source_dir": "",
            "last_output_dir": ""
        }

    def load_user_config(self) -> Optional[Dict[str, Any]]:
        """
        加载用户配置

        Returns:
            Optional[Dict[str, Any]]: 用户配置字典，如果文件不存在返回None
        """
        try:
            if self.user_config_file.exists():
                with open(self.user_config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                return config
            else:
                return None
        except Exception as e:
            print(f"加载用户配置失败: {str(e)}")
            return None

    def save_user_config(self, config: Dict[str, Any]) -> bool:
        """
        保存用户配置

        Args:
            config (Dict[str, Any]): 配置字典

        Returns:
            bool: 保存是否成功
        """
        try:
            # 确保敏感信息不被记录到日志
            safe_config = config.copy()
            if 'api_key' in safe_config:
                safe_config['api_key'] = '***' if safe_config['api_key'] else ''

            with open(self.user_config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存用户配置失败: {str(e)}")
            return False

    def load_api_providers_config(self) -> Dict[str, Any]:
        """
        加载API接口配置

        Returns:
            Dict[str, Any]: API接口配置
        """
        try:
            if self.api_providers_file.exists():
                with open(self.api_providers_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                return config
            else:
                print("API接口配置文件不存在")
                return {}
        except Exception as e:
            print(f"加载API接口配置失败: {str(e)}")
            return {}

    def get_default_config(self) -> Dict[str, Any]:
        """
        获取默认配置

        Returns:
            Dict[str, Any]: 默认配置字典
        """
        return self.default_config.copy()

    def merge_with_defaults(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        将用户配置与默认配置合并

        Args:
            config (Dict[str, Any]): 用户配置

        Returns:
            Dict[str, Any]: 合并后的配置
        """
        merged = self.default_config.copy()
        merged.update(config)
        return merged

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """
        获取配置值

        Args:
            key (str): 配置键
            default (Any): 默认值

        Returns:
            Any: 配置值
        """
        config = self.load_user_config()
        if config:
            return config.get(key, default)
        else:
            return self.default_config.get(key, default)

    def set_config_value(self, key: str, value: Any) -> bool:
        """
        设置配置值

        Args:
            key (str): 配置键
            value (Any): 配置值

        Returns:
            bool: 设置是否成功
        """
        try:
            config = self.load_user_config() or {}
            config[key] = value
            return self.save_user_config(config)
        except Exception as e:
            print(f"设置配置值失败: {str(e)}")
            return False

    def get_api_key(self, provider: str) -> str:
        """
        获取指定接口的API密钥

        Args:
            provider (str): API接口名称

        Returns:
            str: API密钥，如果不存在返回空字符串
        """
        config = self.load_user_config()
        if config and 'api_keys' in config:
            return config['api_keys'].get(provider, '')
        return ''

    def set_api_key(self, provider: str, api_key: str) -> bool:
        """
        设置指定接口的API密钥

        Args:
            provider (str): API接口名称
            api_key (str): API密钥

        Returns:
            bool: 设置是否成功
        """
        try:
            config = self.load_user_config() or {}

            # 确保api_keys字段存在
            if 'api_keys' not in config:
                config['api_keys'] = {}

            # 设置API密钥
            config['api_keys'][provider] = api_key

            return self.save_user_config(config)
        except Exception as e:
            print(f"设置API密钥失败: {str(e)}")
            return False

    def remove_api_key(self, provider: str) -> bool:
        """
        移除指定接口的API密钥

        Args:
            provider (str): API接口名称

        Returns:
            bool: 移除是否成功
        """
        try:
            config = self.load_user_config() or {}

            if 'api_keys' in config and provider in config['api_keys']:
                del config['api_keys'][provider]
                return self.save_user_config(config)

            return True  # 如果密钥不存在，也认为成功
        except Exception as e:
            print(f"移除API密钥失败: {str(e)}")
            return False

    def get_all_api_keys(self) -> Dict[str, str]:
        """
        获取所有API密钥

        Returns:
            Dict[str, str]: 所有接口的API密钥字典
        """
        config = self.load_user_config()
        if config and 'api_keys' in config:
            return config['api_keys'].copy()
        return {}

    def migrate_old_api_key(self) -> bool:
        """
        迁移旧的单一API密钥到新的多接口结构

        Returns:
            bool: 迁移是否成功
        """
        try:
            config = self.load_user_config()
            if not config:
                return True

            # 检查是否有旧的api_key字段
            old_api_key = config.get('api_key', '')
            current_provider = config.get('current_provider', 'openai')

            if old_api_key and 'api_keys' not in config:
                # 迁移旧密钥到当前接口
                config['api_keys'] = {current_provider: old_api_key}

                # 移除旧的api_key字段
                config.pop('api_key', None)

                return self.save_user_config(config)

            return True
        except Exception as e:
            print(f"迁移API密钥失败: {str(e)}")
            return False

    def backup_config(self, backup_name: str = None) -> bool:
        """
        备份配置文件

        Args:
            backup_name (str): 备份文件名，如果为None则使用时间戳

        Returns:
            bool: 备份是否成功
        """
        try:
            from datetime import datetime

            if backup_name is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_name = f"backup_{timestamp}"

            backup_file = self.config_dir / f"{backup_name}.json"

            if self.user_config_file.exists():
                import shutil
                shutil.copy2(self.user_config_file, backup_file)
                return True
            else:
                print("用户配置文件不存在，无法备份")
                return False
        except Exception as e:
            print(f"备份配置失败: {str(e)}")
            return False

    def restore_config(self, backup_file: str) -> bool:
        """
        恢复配置文件

        Args:
            backup_file (str): 备份文件路径

        Returns:
            bool: 恢复是否成功
        """
        try:
            backup_path = Path(backup_file)
            if backup_path.exists():
                import shutil
                shutil.copy2(backup_path, self.user_config_file)
                return True
            else:
                print(f"备份文件不存在: {backup_file}")
                return False
        except Exception as e:
            print(f"恢复配置失败: {str(e)}")
            return False

    def export_config(self, export_path: str) -> bool:
        """
        导出配置到指定路径

        Args:
            export_path (str): 导出路径

        Returns:
            bool: 导出是否成功
        """
        try:
            config = self.load_user_config()
            if config:
                # 移除敏感信息
                export_config = config.copy()
                export_config['api_key'] = ''

                with open(export_path, 'w', encoding='utf-8') as f:
                    json.dump(export_config, f, ensure_ascii=False, indent=2)
                return True
            else:
                print("没有配置可导出")
                return False
        except Exception as e:
            print(f"导出配置失败: {str(e)}")
            return False

    def import_config(self, import_path: str) -> bool:
        """
        从指定路径导入配置

        Args:
            import_path (str): 导入路径

        Returns:
            bool: 导入是否成功
        """
        try:
            import_path = Path(import_path)
            if import_path.exists():
                with open(import_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                # 保留现有的API密钥（如果导入的配置中没有）
                existing_config = self.load_user_config() or {}
                if not config.get('api_key') and existing_config.get('api_key'):
                    config['api_key'] = existing_config['api_key']

                return self.save_user_config(config)
            else:
                print(f"导入文件不存在: {import_path}")
                return False
        except Exception as e:
            print(f"导入配置失败: {str(e)}")
            return False

    def reset_to_defaults(self) -> bool:
        """
        重置为默认配置

        Returns:
            bool: 重置是否成功
        """
        try:
            return self.save_user_config(self.default_config.copy())
        except Exception as e:
            print(f"重置配置失败: {str(e)}")
            return False

    def get_recent_configs(self, limit: int = 5) -> list:
        """
        获取最近的配置备份列表

        Args:
            limit (int): 返回的备份数量限制

        Returns:
            list: 备份文件列表
        """
        try:
            backup_files = []
            for file in self.config_dir.glob("backup_*.json"):
                backup_files.append({
                    'name': file.stem,
                    'path': str(file),
                    'modified': file.stat().st_mtime
                })

            # 按修改时间排序
            backup_files.sort(key=lambda x: x['modified'], reverse=True)
            return backup_files[:limit]
        except Exception as e:
            print(f"获取最近配置失败: {str(e)}")
            return []

    def cleanup_old_backups(self, keep_count: int = 10) -> int:
        """
        清理旧的配置备份

        Args:
            keep_count (int): 保留的备份数量

        Returns:
            int: 删除的备份文件数量
        """
        try:
            backup_files = []
            for file in self.config_dir.glob("backup_*.json"):
                backup_files.append((file.stat().st_mtime, file))

            # 按时间排序，保留最新的keep_count个
            backup_files.sort(key=lambda x: x[0], reverse=True)

            deleted_count = 0
            for timestamp, file in backup_files[keep_count:]:
                file.unlink()
                deleted_count += 1

            return deleted_count
        except Exception as e:
            print(f"清理备份失败: {str(e)}")
            return 0

    def save_custom_providers_config(self, custom_providers: Dict[str, Any]) -> bool:
        """
        保存自定义接口配置

        Args:
            custom_providers (Dict[str, Any]): 自定义接口配置

        Returns:
            bool: 保存是否成功
        """
        try:
            custom_providers_file = self.config_dir / "custom_providers.json"

            # 读取现有配置并更新
            if custom_providers_file.exists():
                with open(custom_providers_file, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            else:
                existing_data = {
                    "custom_providers": {},
                    "metadata": {
                        "version": "1.0",
                        "created_at": "",
                        "description": "自定义API接口配置文件"
                    }
                }

            # 更新自定义接口配置
            existing_data["custom_providers"] = custom_providers

            # 更新元数据
            from datetime import datetime
            existing_data["metadata"]["updated_at"] = datetime.now().isoformat()
            existing_data["metadata"]["total_providers"] = len(custom_providers)

            # 保存文件
            with open(custom_providers_file, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)

            return True
        except Exception as e:
            print(f"保存自定义接口配置失败: {str(e)}")
            return False

    def load_custom_providers_config(self) -> Dict[str, Any]:
        """
        加载自定义接口配置

        Returns:
            Dict[str, Any]: 自定义接口配置
        """
        try:
            custom_providers_file = self.config_dir / "custom_providers.json"
            if custom_providers_file.exists():
                with open(custom_providers_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data.get("custom_providers", {})
            else:
                return {}
        except Exception as e:
            print(f"加载自定义接口配置失败: {str(e)}")
            return {}

    def import_custom_providers_from_file(self, import_path: str) -> bool:
        """
        从文件导入自定义接口配置

        Args:
            import_path (str): 导入文件路径

        Returns:
            bool: 导入是否成功
        """
        try:
            import_path = Path(import_path)
            if import_path.exists():
                with open(import_path, 'r', encoding='utf-8') as f:
                    import_data = json.load(f)

                # 获取自定义接口配置
                custom_providers = import_data.get("custom_providers", {})

                # 保存到本地配置
                return self.save_custom_providers_config(custom_providers)
            else:
                print(f"导入文件不存在: {import_path}")
                return False
        except Exception as e:
            print(f"从文件导入自定义接口配置失败: {str(e)}")
            return False

    def export_custom_providers_to_file(self, export_path: str) -> bool:
        """
        导出自定义接口配置到文件

        Args:
            export_path (str): 导出文件路径

        Returns:
            bool: 导出是否成功
        """
        try:
            custom_providers = self.load_custom_providers_config()

            export_data = {
                "custom_providers": custom_providers,
                "exported_at": str(Path(export_path).stem),
                "version": "1.0"
            }

            with open(export_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)

            return True
        except Exception as e:
            print(f"导出自定义接口配置失败: {str(e)}")
            return False

    # ===== 系统提示词管理 =====

    def get_default_system_prompts(self) -> Dict[str, Any]:
        """
        获取默认系统提示词配置

        Returns:
            Dict[str, Any]: 默认系统提示词配置
        """
        return {
            "prompts": {
                "标准翻译": {
                    "name": "标准翻译",
                    "description": "适用于一般翻译任务的通用提示词",
                    "content": """你是一个专业的、严格遵守格式的{source_lang}翻译引擎。你的任务是将{source_lang}字幕文件文本翻译成{target_lang}，同时严格保持输入和输出的行数一一对应。

### 核心铁律（最高优先级规则）
1. 【绝对严格】输入和输出的格式必须完全相同。如果输入是 "行号: 文本"，输出也必须是 "行号: 译文"。
2. 【绝对严格】严禁合并或拆分行：即使原文的多行在语义上是一个完整的句子，你也必须在最终输出时保持独立的行。
3. 【绝对严格】必须保留每行的序号：每行开头的数字序号（如 "101:", "102:" 等）必须在输出中完整保留，不能省略或修改。
4. 失败后果：你的输出将被一个自动化脚本按行号进行匹配。任何行数的变动都会导致整个流程失败。你必须模拟这个脚本的行为，对每一行进行独立处理和输出。

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

请严格按照以上规则进行翻译，确保每一行都有对应的翻译结果。""",
                    "tags": ["通用", "日常"],
                    "is_default": True
                },
                "专业翻译": {
                    "name": "专业翻译",
                    "description": "适用于专业领域内容的翻译，注重术语准确性",
                    "content": """你是一个专业的{source_lang}到{target_lang}翻译专家，专门处理专业领域内容的翻译。你的任务是将{source_lang}字幕文件文本翻译成{target_lang}，同时严格保持输入和输出的行数一一对应。

### 核心要求
1. 严格保持原有的行号格式和行数，确保每行都有对应的翻译
2. 专业术语要准确统一，必要时保持原文术语并在括号中加注释
3. 保持原文的专业性和正式性
4. 确保翻译结果在专业领域内准确且易懂

### 翻译原则
- 术语一致性：相同的专业概念使用统一的翻译
- 准确性优先：忠实传达原文的专业含义
- 可读性：译文要符合{target_lang}的专业表达习惯
- 格式保持：严格维持原有的行号结构

请按照以上要求进行翻译，确保专业内容的准确传达。""",
                    "tags": ["专业", "技术", "学术"],
                    "is_default": False
                },
                "口语化翻译": {
                    "name": "口语化翻译",
                    "description": "适用于日常对话内容的自然口语翻译",
                    "content": """你是一个地道的{target_lang}母语者，擅长将{source_lang}内容自然地翻译成{target_lang}口语。你的任务是将{source_lang}字幕文件文本翻译成{target_lang}，同时严格保持输入和输出的行数一一对应。

### 翻译风格要求
1. 自然流畅：翻译要像地道的{target_lang}口语表达
2. 生活化：使用日常对话中常见的词汇和表达方式
3. 情感传达：保持原文的情感色彩和语气
4. 语境适应：根据对话场景选择合适的表达

### 注意事项
- 保持原有的行号格式，确保每行对应
- 避免过于书面化或生硬的表达
- 适当使用口语化的语气词和表达
- 确保翻译结果听起来自然、不生硬

请用你最自然的口语风格进行翻译，让译文听起来就像母语者的日常对话。""",
                    "tags": ["口语", "日常", "自然"],
                    "is_default": False
                }
            },
            "current_prompt": "标准翻译",
            "version": "1.0"
        }

    def load_system_prompts(self) -> Dict[str, Any]:
        """
        加载系统提示词配置

        Returns:
            Dict[str, Any]: 系统提示词配置
        """
        try:
            if self.system_prompts_file.exists():
                with open(self.system_prompts_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                return config
            else:
                # 如果文件不存在，创建默认配置
                default_config = self.get_default_system_prompts()
                self.save_system_prompts(default_config)
                return default_config
        except Exception as e:
            print(f"加载系统提示词配置失败: {str(e)}")
            return self.get_default_system_prompts()

    def save_system_prompts(self, config: Dict[str, Any]) -> bool:
        """
        保存系统提示词配置

        Args:
            config (Dict[str, Any]): 系统提示词配置

        Returns:
            bool: 保存是否成功
        """
        try:
            # 确保目录存在
            self.system_prompts_file.parent.mkdir(exist_ok=True)

            with open(self.system_prompts_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存系统提示词配置失败: {str(e)}")
            return False

    def get_system_prompt(self, prompt_name: str) -> Optional[Dict[str, Any]]:
        """
        获取指定的系统提示词

        Args:
            prompt_name (str): 提示词名称

        Returns:
            Optional[Dict[str, Any]]: 提示词配置，如果不存在返回None
        """
        config = self.load_system_prompts()
        prompts = config.get("prompts", {})
        return prompts.get(prompt_name)

    def add_system_prompt(self, prompt_name: str, prompt_data: Dict[str, Any]) -> bool:
        """
        添加系统提示词

        Args:
            prompt_name (str): 提示词名称
            prompt_data (Dict[str, Any]): 提示词数据

        Returns:
            bool: 添加是否成功
        """
        try:
            config = self.load_system_prompts()

            # 如果不存在prompts字段，创建它
            if "prompts" not in config:
                config["prompts"] = {}

            # 添加新的提示词
            config["prompts"][prompt_name] = prompt_data

            return self.save_system_prompts(config)
        except Exception as e:
            print(f"添加系统提示词失败: {str(e)}")
            return False

    def update_system_prompt(self, prompt_name: str, prompt_data: Dict[str, Any]) -> bool:
        """
        更新系统提示词

        Args:
            prompt_name (str): 提示词名称
            prompt_data (Dict[str, Any]): 新的提示词数据

        Returns:
            bool: 更新是否成功
        """
        return self.add_system_prompt(prompt_name, prompt_data)  # 逻辑相同

    def delete_system_prompt(self, prompt_id: str) -> bool:
        """
        删除系统提示词

        Args:
            prompt_id (str): 提示词ID

        Returns:
            bool: 删除是否成功
        """
        try:
            config = self.load_system_prompts()
            prompts = config.get("prompts", {})

            if prompt_id in prompts:
                # 获取提示词名称用于日志
                prompt_name = prompts[prompt_id].get('name', prompt_id)

                # 删除提示词
                del prompts[prompt_id]

                # 如果删除的是当前使用的提示词，切换到默认提示词
                if config.get("current_prompt") == prompt_id:
                    # 找到第一个默认提示词
                    for name, data in prompts.items():
                        if data.get("is_default", False):
                            config["current_prompt"] = name
                            break
                    else:
                        # 如果没有默认提示词，使用第一个
                        if prompts:
                            config["current_prompt"] = list(prompts.keys())[0]
                        else:
                            config["current_prompt"] = ""

                print(f"成功删除系统提示词: {prompt_name} (ID: {prompt_id})")
                return self.save_system_prompts(config)
            else:
                print(f"提示词不存在: {prompt_id}")
                return True  # 提示词不存在，也认为成功
        except Exception as e:
            print(f"删除系统提示词失败: {str(e)}")
            return False

    def get_all_system_prompts(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有系统提示词

        Returns:
            Dict[str, Dict[str, Any]]: 所有提示词的字典
        """
        config = self.load_system_prompts()
        return config.get("prompts", {})

    def get_current_system_prompt(self) -> str:
        """
        获取当前使用的系统提示词名称

        Returns:
            str: 当前提示词名称
        """
        config = self.load_system_prompts()
        return config.get("current_prompt", "标准翻译")

    def set_current_system_prompt(self, prompt_name: str) -> bool:
        """
        设置当前使用的系统提示词

        Args:
            prompt_name (str): 提示词名称

        Returns:
            bool: 设置是否成功
        """
        try:
            config = self.load_system_prompts()
            prompts = config.get("prompts", {})

            if prompt_name in prompts:
                config["current_prompt"] = prompt_name
                return self.save_system_prompts(config)
            else:
                print(f"提示词不存在: {prompt_name}")
                return False
        except Exception as e:
            print(f"设置当前系统提示词失败: {str(e)}")
            return False

    def export_system_prompts(self, export_path: str) -> bool:
        """
        导出系统提示词配置

        Args:
            export_path (str): 导出路径

        Returns:
            bool: 导出是否成功
        """
        try:
            config = self.load_system_prompts()

            export_data = {
                "system_prompts": config,
                "exported_at": str(Path(export_path).stem),
                "version": "1.0"
            }

            with open(export_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)

            return True
        except Exception as e:
            print(f"导出系统提示词配置失败: {str(e)}")
            return False

    def import_system_prompts(self, import_path: str, merge: bool = True) -> bool:
        """
        导入系统提示词配置

        Args:
            import_path (str): 导入文件路径
            merge (bool): 是否合并现有配置，False则覆盖

        Returns:
            bool: 导入是否成功
        """
        try:
            import_path = Path(import_path)
            if import_path.exists():
                with open(import_path, 'r', encoding='utf-8') as f:
                    import_data = json.load(f)

                # 获取导入的提示词配置
                imported_prompts = import_data.get("system_prompts", {})

                if merge:
                    # 合并模式：保留现有配置，添加新的提示词
                    current_config = self.load_system_prompts()
                    current_prompts = current_config.get("prompts", {})

                    # 合并提示词，导入的不会覆盖现有的同名提示词
                    for name, data in imported_prompts.get("prompts", {}).items():
                        if name not in current_prompts:
                            current_prompts[name] = data

                    current_config["prompts"] = current_prompts
                    return self.save_system_prompts(current_config)
                else:
                    # 覆盖模式：直接使用导入的配置
                    return self.save_system_prompts(imported_prompts)
            else:
                print(f"导入文件不存在: {import_path}")
                return False
        except Exception as e:
            print(f"导入系统提示词配置失败: {str(e)}")
            return False

    def get_optimal_concurrency(self, provider: str) -> int:
        """
        获取指定接口的最优并发数

        Args:
            provider (str): API接口名称

        Returns:
            int: 最优并发数
        """
        # 预定义的各接口推荐并发数
        provider_concurrency = {
            "openai": 3,        # OpenAI官方建议不超过3-5
            "claude": 2,        # Anthropic限制较严格
            "qwen": 4,          # 阿里云通义千问
            "wenxin": 3,        # 百度文心一言
            "zhipu": 3,         # 智谱AI
            "deepseek": 5,      # DeepSeek限制较宽松
            "moonshot": 3,      # Moonshot AI
            "custom": 1         # 自定义接口默认保守设置
        }

        # 如果是自定义接口，检查是否有本地地址
        if provider.startswith("custom_") or provider not in provider_concurrency:
            # 检查是否为本地地址（localhost, 127.0.0.1等）
            return 4  # 本地模型通常支持更高并发

        return provider_concurrency.get(provider, 2)

    def get_effective_concurrency(self, provider: str, user_setting: int) -> int:
        """
        获取有效的并发数设置

        Args:
            provider (str): API接口名称
            user_setting (int): 用户设置的并发数（0表示自动）

        Returns:
            int: 有效的并发数
        """
        if user_setting == 0:
            # 自动模式：使用推荐值
            return self.get_optimal_concurrency(provider)
        else:
            # 用户手动设置，确保不超过合理范围
            max_concurrency = 8  # 最大并发数限制
            return min(max(1, user_setting), max_concurrency)

    def get_concurrency(self) -> int:
        """
        获取并发任务数设置

        Returns:
            int: 并发任务数，0表示自动模式
        """
        config = self.load_user_config()
        return config.get('concurrency', 0) if config else 0

    def set_concurrency(self, concurrency: int) -> bool:
        """
        设置并发任务数

        Args:
            concurrency (int): 并发任务数，0表示自动模式

        Returns:
            bool: 设置是否成功
        """
        try:
            config = self.load_user_config() or {}
            config['concurrency'] = max(0, min(8, concurrency))  # 限制在0-8之间
            return self.save_user_config(config)
        except Exception as e:
            print(f"设置并发数失败: {str(e)}")
            return False

    def get_timeout(self) -> int:
        """
        获取请求超时时间设置

        Returns:
            int: 超时时间（秒），默认240秒
        """
        config = self.load_user_config()
        return config.get('timeout', 240) if config else 240

    def set_timeout(self, timeout: int) -> bool:
        """
        设置请求超时时间

        Args:
            timeout (int): 超时时间（秒），30-600秒之间

        Returns:
            bool: 设置是否成功
        """
        try:
            config = self.load_user_config() or {}
            config['timeout'] = max(30, min(600, timeout))  # 限制在30-600秒之间
            return self.save_user_config(config)
        except Exception as e:
            print(f"设置超时时间失败: {str(e)}")
            return False
