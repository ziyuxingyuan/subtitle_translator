#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译状态管理器
负责管理翻译进度的持久化，支持断点续传功能。
状态文件默认存放在当前配置根目录同级的 resume_states 下（兼容 legacy data/resume_states）。
"""

import json
import hashlib
import os
from pathlib import Path
from typing import Dict, List, Any, Optional
import threading
from datetime import datetime

from modules.config_paths import get_target_config_dir


class TranslationStateManager:
    """翻译状态管理器"""

    def __init__(self, source_file_path: str):
        """
        初始化状态管理器

        Args:
            source_file_path (str): 源文件路径
        """
        self.source_file_path = Path(source_file_path)
        self._legacy_state_root = Path("data") / "resume_states"
        try:
            self._state_root = get_target_config_dir().parent / "resume_states"
        except Exception:
            self._state_root = self._legacy_state_root
        self._legacy_state_file_path = self.source_file_path.with_suffix(self.source_file_path.suffix + ".progress")
        self.state_file_path = self._build_state_file_path()
        self._migrate_legacy_state()
        self.lock = threading.RLock()  # 可重入锁，避免嵌套调用时死锁

    def _build_state_file_path(self) -> Path:
        self._state_root.mkdir(parents=True, exist_ok=True)
        path_key = str(self.source_file_path.absolute())
        name_hash = hashlib.sha256(path_key.encode("utf-8")).hexdigest()[:16]
        return self._state_root / f"{name_hash}.progress"

    def _migrate_legacy_state(self) -> None:
        if self.state_file_path.exists():
            return
        legacy_hashed = self._legacy_state_root / self.state_file_path.name
        candidates: list[tuple[Path, bool]] = []
        if legacy_hashed.exists():
            candidates.append((legacy_hashed, True))
        if self._legacy_state_file_path.exists():
            candidates.append((self._legacy_state_file_path, True))

        for source_path, delete_after_copy in candidates:
            try:
                self.state_file_path.parent.mkdir(parents=True, exist_ok=True)
                content = source_path.read_bytes()
                self.state_file_path.write_bytes(content)
                if delete_after_copy:
                    try:
                        source_path.unlink()
                    except Exception:
                        pass
                return
            except Exception:
                continue

        if self._legacy_state_file_path.exists():
            self.state_file_path = self._legacy_state_file_path

    def _calculate_file_hash(self, file_path: Path) -> str:
        """
        计算文件的SHA256哈希值

        Args:
            file_path (Path): 文件路径

        Returns:
            str: 文件的SHA256哈希值
        """
        hash_sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            # 分块读取大文件，避免内存占用过高
            for chunk in iter(lambda: f.read(8192), b""):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()

    def save_state(self, translated_blocks: Dict[str, Any], total_blocks: int,
                   source_file_hash: str) -> bool:
        """
        保存翻译状态到文件

        Args:
            translated_blocks (Dict[str, Any]): 已翻译的字幕块字典，键为字符串格式的索引
            total_blocks (int): 总字幕块数量
            source_file_hash (str): 源文件哈希值

        Returns:
            bool: 保存是否成功
        """
        with self.lock:
            try:
                # 创建状态数据
                state_data = {
                    "metadata": {
                        "source_file_hash": source_file_hash,
                        "total_blocks": total_blocks,
                        "last_update": datetime.now().isoformat()
                    },
                    "translated_blocks": translated_blocks
                }

                # 确保父目录存在
                self.state_file_path.parent.mkdir(parents=True, exist_ok=True)

                # 原子写入：先写到临时文件，再重命名
                temp_path = self.state_file_path.with_suffix('.tmp')
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(state_data, f, ensure_ascii=False, indent=2)

                # 重命名临时文件为正式状态文件
                temp_path.replace(self.state_file_path)
                return True

            except Exception as e:
                print(f"保存翻译状态失败: {str(e)}")
                return False

    def load_state(self) -> Optional[Dict[str, Any]]:
        """
        从文件加载翻译状态

        Returns:
            Optional[Dict[str, Any]]: 状态数据，如果文件不存在或格式错误则返回None
        """
        with self.lock:
            if not self.state_file_path.exists():
                return None

            try:
                with open(self.state_file_path, 'r', encoding='utf-8') as f:
                    state_data = json.load(f)

                # 验证数据格式
                if not isinstance(state_data, dict):
                    print("状态文件格式错误：不是有效的JSON对象")
                    return None

                if "metadata" not in state_data or "translated_blocks" not in state_data:
                    print("状态文件格式错误：缺少必要的字段")
                    return None

                # 验证已翻译块是否为字典格式
                if not isinstance(state_data["translated_blocks"], dict):
                    print("状态文件格式错误：translated_blocks不是字典格式")
                    return None

                return state_data

            except Exception as e:
                print(f"加载翻译状态失败: {str(e)}")
                return None

    def has_valid_state(self) -> bool:
        """
        检查是否存在有效的翻译状态

        Returns:
            bool: 是否存在有效的翻译状态
        """
        state_data = self.load_state()
        return state_data is not None

    def validate_source_file(self) -> tuple[bool, str]:
        """
        验证源文件是否与保存的状态匹配（基于哈希值）

        Returns:
            tuple[bool, str]: (是否匹配, 不匹配的原因或空字符串)
        """
        if not self.source_file_path.exists():
            return False, "源文件不存在"

        state_data = self.load_state()
        if not state_data:
            return False, "没有保存的翻译状态"

        saved_hash = state_data["metadata"].get("source_file_hash", "")
        is_auto_recovered = bool(state_data["metadata"].get("auto_recovered", False))

        # 自愈状态可能缺少可靠哈希，避免误导为“源文件已修改”。
        if is_auto_recovered and not saved_hash:
            return False, "状态文件为自动重建且缺少源文件哈希，无法验证一致性，请重新开始翻译"

        current_hash = self._calculate_file_hash(self.source_file_path)

        if current_hash != saved_hash:
            if is_auto_recovered:
                return False, "状态文件为自动重建且源文件与恢复快照不一致，请重新开始翻译"
            return False, "源文件已修改，请重新开始翻译"

        return True, ""

    def get_untranslated_blocks(self, all_original_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        获取需要翻译的字幕块列表

        Args:
            all_original_blocks (List[Dict[str, Any]]): 所有原始字幕块

        Returns:
            List[Dict[str, Any]]: 需要翻译的字幕块列表
        """
        state_data = self.load_state()
        if not state_data:
            # 没有状态文件，返回所有原始块
            return all_original_blocks

        translated_blocks = state_data.get("translated_blocks", {})
        translated_indices = set(translated_blocks.keys())

        # 找出未翻译的块
        untranslated_blocks = []
        for block in all_original_blocks:
            index_str = str(block.get('index', 0))
            if index_str not in translated_indices:
                untranslated_blocks.append(block)

        return untranslated_blocks

    def get_translated_blocks_count(self) -> int:
        """
        获取已翻译的块数量

        Returns:
            int: 已翻译的块数量
        """
        state_data = self.load_state()
        if not state_data:
            return 0

        translated_blocks = state_data.get("translated_blocks", {})
        return len(translated_blocks)

    def save_batch(self, translated_batch: List[Dict[str, Any]]) -> bool:
        """
        保存翻译批次到状态文件

        Args:
            translated_batch (List[Dict[str, Any]]): 翻译批次

        Returns:
            bool: 保存是否成功
        """
        if not translated_batch:
            return True

        with self.lock:  # 确保线程安全
            # 加载当前状态
            state_data = self.load_state()
            if not state_data:
                # 状态缺失时执行自愈：至少保证当前批次可持久化
                recovered_total = 0
                for block in translated_batch:
                    try:
                        recovered_total = max(recovered_total, int(block.get("index", 0)))
                    except Exception:
                        continue
                if recovered_total <= 0:
                    recovered_total = len(translated_batch)
                source_hash = ""
                try:
                    if self.source_file_path.exists():
                        source_hash = self._calculate_file_hash(self.source_file_path)
                except Exception:
                    source_hash = ""
                state_data = {
                    "metadata": {
                        "source_file_hash": source_hash,
                        "total_blocks": recovered_total,
                        "last_update": datetime.now().isoformat(),
                        "auto_recovered": True,
                    },
                    "translated_blocks": {},
                }
                print("警告：翻译状态文件缺失或损坏，已自动重建状态文件。")

            # 更新已翻译的块
            translated_blocks = state_data.get("translated_blocks", {})
            for block in translated_batch:
                index_str = str(block.get('index', 0))
                translated_blocks[index_str] = block

            # 更新元数据
            state_data["metadata"]["last_update"] = datetime.now().isoformat()
            try:
                total_blocks_value = int(state_data["metadata"].get("total_blocks", 0))
            except Exception:
                total_blocks_value = 0
            if total_blocks_value <= 0:
                state_data["metadata"]["total_blocks"] = len(translated_blocks)
            state_data["translated_blocks"] = translated_blocks

            # 保存状态
            return self.save_state(
                state_data["translated_blocks"],
                state_data["metadata"]["total_blocks"],
                state_data["metadata"]["source_file_hash"]
            )

    def get_all_blocks_for_rebuild(self, all_original_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        获取用于重建SRT文件的完整块列表
        将已翻译的块和未翻译的原始块合并，保持原始顺序

        Args:
            all_original_blocks (List[Dict[str, Any]]): 所有原始字幕块

        Returns:
            List[Dict[str, Any]]: 完整的块列表，包含已翻译和未翻译的内容
        """
        state_data = self.load_state()
        if not state_data:
            # 没有状态文件，返回原始块
            return all_original_blocks

        translated_blocks = state_data.get("translated_blocks", {})
        result_blocks = []

        # 按原始顺序重建块列表
        for original_block in all_original_blocks:
            index_str = str(original_block.get('index', 0))
            if index_str in translated_blocks:
                # 使用已翻译的块
                result_blocks.append(translated_blocks[index_str])
            else:
                # 使用原始块（未翻译）
                result_blocks.append(original_block)

        return result_blocks

    def get_total_blocks_info(self) -> tuple[int, int, int]:
        """
        获取总块数信息

        Returns:
            tuple[int, int, int]: (总块数, 已翻译块数, 未翻译块数)
        """
        state_data = self.load_state()
        if not state_data:
            return 0, 0, 0

        total_blocks = state_data["metadata"].get("total_blocks", 0)
        translated_count = len(state_data.get("translated_blocks", {}))
        untranslated_count = total_blocks - translated_count

        return total_blocks, translated_count, untranslated_count

    def cleanup(self):
        """
        清理状态文件（翻译完成后调用）
        """
        try:
            if self.state_file_path.exists():
                self.state_file_path.unlink()
                print(f"已删除状态文件: {self.state_file_path}")
        except Exception as e:
            print(f"删除状态文件失败: {str(e)}")

    def start_new_translation(self, all_original_blocks: List[Dict[str, Any]]) -> bool:
        """
        开始新的翻译任务，创建初始状态文件

        Args:
            all_original_blocks (List[Dict[str, Any]]): 所有原始字幕块

        Returns:
            bool: 初始化是否成功
        """
        try:
            source_hash = self._calculate_file_hash(self.source_file_path)
            total_blocks = len(all_original_blocks)

            # 创建初始状态（空的翻译块）
            initial_translated_blocks = {}

            success = self.save_state(initial_translated_blocks, total_blocks, source_hash)
            if success:
                print(f"创建新的翻译状态文件: {self.state_file_path}")
            return success

        except Exception as e:
            print(f"初始化翻译状态失败: {str(e)}")
            return False
