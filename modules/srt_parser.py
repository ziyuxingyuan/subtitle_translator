#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRT字幕文件解析器
支持标准SRT格式的解析、验证和重建
"""

import re
from typing import Any, List, Dict, Optional
from pathlib import Path

from modules.config_paths import get_config_dir


class SRTParser:
    """SRT字幕文件解析器"""

    def __init__(self):
        # SRT字幕块的正则表达式模式
        self.block_pattern = re.compile(r'(\d+)\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*([\s\S]*?)(?=\n\n|\Z)', re.MULTILINE)

        # 时间轴格式验证
        self.timestamp_pattern = re.compile(r'^\d{2}:\d{2}:\d{2},\d{3}$')

    def parse(self, content: str) -> List[Dict[str, Any]]:
        """
        解析SRT文件内容

        Args:
            content (str): SRT文件内容

        Returns:
            List[Dict[str, Any]]: 解析后的字幕块列表
        """
        # 清理内容，移除BOM和多余空白
        content = content.strip()
        if content.startswith('\ufeff'):
            content = content[1:]  # 移除UTF-8 BOM

        # 查找所有字幕块
        blocks = self.block_pattern.findall(content)

        if not blocks:
            raise ValueError("未找到有效的SRT字幕块，请检查文件格式")

        subtitle_blocks = []

        for i, block in enumerate(blocks):
            try:
                index, start_time, end_time, text = block

                # 验证索引
                index = int(index.strip())

                # 验证时间轴格式
                if not self._validate_timestamp(start_time.strip()):
                    raise ValueError(f"无效的开始时间格式: {start_time}")

                if not self._validate_timestamp(end_time.strip()):
                    raise ValueError(f"无效的结束时间格式: {end_time}")

                # 清理文本内容
                text = text.strip()
                if not text:
                    raise ValueError(f"字幕块 {index} 的文本内容为空")

                # 创建字幕块字典
                subtitle_block = {
                    'index': index,
                    'start_time': start_time.strip(),
                    'end_time': end_time.strip(),
                    'timestamp': f"{start_time.strip()} --> {end_time.strip()}",
                    'text': text,
                    'original_text': text  # 保存原始文本
                }

                subtitle_blocks.append(subtitle_block)

            except Exception as e:
                raise ValueError(f"解析字幕块 {i+1} 时出错: {str(e)}")

        # 按索引排序
        subtitle_blocks.sort(key=lambda x: x['index'])

        # 验证索引连续性
        self._validate_index_continuity(subtitle_blocks)

        return subtitle_blocks

    def _validate_timestamp(self, timestamp: str) -> bool:
        """验证时间戳格式"""
        return bool(self.timestamp_pattern.match(timestamp))

    def _validate_index_continuity(self, blocks: List[Dict[str, Any]]):
        """验证索引连续性"""
        for i, block in enumerate(blocks):
            expected_index = i + 1
            if block['index'] != expected_index:
                print(f"警告: 字幕块索引不连续，期望 {expected_index}，实际 {block['index']}")

    def rebuild(self, blocks: List[Dict[str, Any]]) -> str:
        """
        重建SRT文件内容

        Args:
            blocks (List[Dict[str, Any]]): 字幕块列表

        Returns:
            str: 重建的SRT文件内容
        """
        if not blocks:
            raise ValueError("字幕块列表为空")

        srt_lines = []

        for i, block in enumerate(blocks):
            try:
                # 确保索引正确
                index = i + 1

                # 获取时间轴信息
                start_time = block.get('start_time') or block.get('time_start')
                end_time = block.get('end_time') or block.get('time_end')

                if not start_time or not end_time:
                    timestamp = block.get('timestamp', '')
                    if '-->' in timestamp:
                        start_time, end_time = timestamp.split(' --> ')
                    else:
                        raise ValueError(f"字幕块 {index} 缺少时间轴信息")

                # 获取翻译后的文本
                translated_text = block.get('text', '').strip()
                if not translated_text:
                    translated_text = block.get('translated_text', '').strip()

                if not translated_text:
                    print(f"警告: 字幕块 {index} 的翻译文本为空，使用原始文本")
                    translated_text = block.get('original_text', '').strip()

                # 构建SRT块
                srt_lines.append(str(index))
                srt_lines.append(f"{start_time.strip()} --> {end_time.strip()}")
                srt_lines.append(translated_text)
                srt_lines.append("")  # 空行分隔

            except Exception as e:
                raise ValueError(f"重建字幕块 {i+1} 时出错: {str(e)}")

        # 确保文件以换行符结尾
        srt_content = '\n'.join(srt_lines)
        if not srt_content.endswith('\n'):
            srt_content += '\n'

        return srt_content

    def validate_srt_format(self, content: str) -> tuple[bool, List[str]]:
        """
        验证SRT文件格式

        Args:
            content (str): SRT文件内容

        Returns:
            tuple[bool, List[str]]: (是否有效, 错误信息列表)
        """
        errors = []

        try:
            blocks = self.parse(content)

            if not blocks:
                errors.append("未找到有效的字幕块")
                return False, errors

            # 检查每个字幕块
            for i, block in enumerate(blocks):
                # 检查索引
                if not isinstance(block.get('index'), int) or block['index'] <= 0:
                    errors.append(f"字幕块 {i+1}: 无效的索引")

                # 检查时间轴
                start_time = block.get('start_time', '')
                end_time = block.get('end_time', '')

                if not self._validate_timestamp(start_time):
                    errors.append(f"字幕块 {i+1}: 无效的开始时间格式")

                if not self._validate_timestamp(end_time):
                    errors.append(f"字幕块 {i+1}: 无效的结束时间格式")

                # 检查时间逻辑
                if start_time and end_time and not self._validate_time_sequence(start_time, end_time):
                    errors.append(f"字幕块 {i+1}: 开始时间晚于结束时间")

                # 检查文本内容
                text = block.get('text', '').strip()
                if not text:
                    errors.append(f"字幕块 {i+1}: 文本内容为空")

            return len(errors) == 0, errors

        except Exception as e:
            errors.append(f"验证过程中出错: {str(e)}")
            return False, errors

    def _validate_time_sequence(self, start_time: str, end_time: str) -> bool:
        """验证时间序列（开始时间应该早于结束时间）"""
        try:
            # 将时间戳转换为毫秒数进行比较
            start_ms = self._timestamp_to_milliseconds(start_time)
            end_ms = self._timestamp_to_milliseconds(end_time)
            return start_ms < end_ms
        except:
            return False

    @staticmethod
    def time_to_milliseconds(timestamp: str) -> int:
        """Convert HH:MM:SS,mmm timestamp to milliseconds."""
        parts = timestamp.split(':')
        if len(parts) != 3:
            raise ValueError(f"Invalid timestamp format: {timestamp}")

        hours = int(parts[0])
        minutes = int(parts[1])
        seconds_millis = parts[2].split(',')
        if len(seconds_millis) != 2:
            raise ValueError(f"Invalid timestamp format: {timestamp}")

        seconds = int(seconds_millis[0])
        milliseconds = int(seconds_millis[1])
        return (hours * 3600 + minutes * 60 + seconds) * 1000 + milliseconds

    @staticmethod
    def milliseconds_to_time(ms: int) -> str:
        """Convert milliseconds to HH:MM:SS,mmm timestamp."""
        if ms < 0:
            ms = 0
        hours = ms // 3600000
        minutes = (ms % 3600000) // 60000
        seconds = (ms % 60000) // 1000
        milliseconds = ms % 1000
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

    def _timestamp_to_milliseconds(self, timestamp: str) -> int:
        """将时间戳转换为毫秒数"""
        return self.time_to_milliseconds(timestamp)

    def merge_translations(self, original_blocks: List[Dict[str, Any]],
                          translations: List[str]) -> List[Dict[str, Any]]:
        """
        将翻译结果合并到原始字幕块中

        Args:
            original_blocks (List[Dict[str, Any]]): 原始字幕块列表
            translations (List[str]): 翻译结果列表

        Returns:
            List[Dict[str, Any]]: 合并后的字幕块列表
        """
        if len(original_blocks) != len(translations):
            raise ValueError(f"翻译结果数量({len(translations)})与原始字幕块数量({len(original_blocks)})不匹配")

        merged_blocks = []

        for i, (original_block, translation) in enumerate(zip(original_blocks, translations)):
            merged_block = original_block.copy()
            merged_block['translated_text'] = translation.strip()
            merged_block['text'] = translation.strip()  # 使用翻译后的文本作为显示文本
            merged_blocks.append(merged_block)

        return merged_blocks

    def extract_text_for_translation(self, blocks: List[Dict[str, Any]],
                                   start_index: int = 1) -> List[str]:
        """
        提取文本内容用于翻译

        Args:
            blocks (List[Dict[str, Any]]): 字幕块列表
            start_index (int): 起始索引（用于编号）

        Returns:
            List[str]: 带编号的文本列表
        """
        texts = []

        for i, block in enumerate(blocks):
            text = block.get('text', '').strip()
            if text:
                # 添加行号前缀，便于翻译结果匹配
                numbered_text = f"{start_index + i}: {text}"
                texts.append(numbered_text)

        return texts

    def parse_translation_response(self, response: str, expected_count: int) -> List[str]:
        """
        解析翻译响应

        Args:
            response (str): API翻译响应
            expected_count (int): 期望的翻译条数

        Returns:
            List[str]: 解析后的翻译结果列表
        """
        if not response:
            raise ValueError("翻译响应为空")

        # 按行分割响应
        lines = response.strip().split('\n')

        # 清理并过滤行，移除明显的非翻译内容
        cleaned_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 跳过常见的非翻译行
            if any(skip_text in line.lower() for skip_text in [
                '输入：', '输出：', 'example', '示例', '###', '核心铁律', '翻译流程',
                '第一步', '第二步', '第三步', '请严格按照', '确保每一行'
            ]):
                continue

            cleaned_lines.append(line)

        # 尝试提取包含序号的翻译行（优先匹配）
        numbered_lines = []
        for line in cleaned_lines:
            # 匹配 "数字:内容" 或 "数字.内容" 格式
            if re.match(r'^\s*\d+\s*[:.]\s*.*', line):
                numbered_lines.append(line)

        # 如果提取到的序号行数量与期望相符，使用它们
        if len(numbered_lines) == expected_count:
            lines_to_process = numbered_lines
        elif len(numbered_lines) > 0 and len(numbered_lines) <= expected_count:
            # 如果序号行少于期望但非零，可能只有部分返回，使用序号行
            lines_to_process = numbered_lines
        else:
            # 否则使用所有清理后的行，并尝试智能匹配
            lines_to_process = cleaned_lines

        # 如果处理后的行数仍不匹配，尝试更智能的提取
        if len(lines_to_process) != expected_count:
            # 如果AI返回了过多内容（如重复示例），尝试截取正确数量的行
            if len(lines_to_process) > expected_count:
                # 优先保留序号行，如果有的话
                valid_numbered_lines = []
                for line in lines_to_process:
                    if re.match(r'^\s*\d+\s*[:.]\s*.*', line):
                        valid_numbered_lines.append(line)

                # 如果序号行数量正确，使用序号行
                if len(valid_numbered_lines) == expected_count:
                    lines_to_process = valid_numbered_lines
                elif len(valid_numbered_lines) > 0:
                    # 如果序号行部分匹配，补充其他行
                    lines_to_process = valid_numbered_lines
                else:
                    # 否则截取前expected_count行
                    lines_to_process = lines_to_process[:expected_count]
            else:
                # 如果行数少于期望，抛出错误（这通常是真正的问题）
                raise ValueError(f"翻译结果行数({len(lines_to_process)})与期望行数({expected_count})不匹配")

        translations = []
        for line in lines_to_process:
            # 移除行号前缀（格式：序号: 翻译文本 或 序号. 翻译文本）
            if ':' in line:
                parts = line.split(':', 1)
                if len(parts) == 2:
                    translation = parts[1].strip()
                else:
                    translation = line.strip()
            elif '.' in line and line.strip() and line.strip()[0].isdigit():
                # 检查是否是数字开头的行，如果是句点分隔的序号格式
                parts = line.split('.', 1)
                if len(parts) == 2:
                    translation = parts[1].strip()
                else:
                    translation = line.strip()
            else:
                translation = line.strip()

            translations.append(translation)

        return translations

    def save_debug_files(self, blocks: List[Dict[str, Any]],
                        translations: List[str],
                        debug_dir: str | None = None):
        """
        保存调试文件

        Args:
            blocks (List[Dict[str, Any]]): 原始字幕块
            translations (List[str]): 翻译结果
            debug_dir (str): 调试文件目录
        """
        from datetime import datetime
        import os

        if not debug_dir:
            debug_dir = str(get_config_dir() / "debug_files")

        # 创建调试目录
        Path(debug_dir).mkdir(parents=True, exist_ok=True)

        # 生成时间戳
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 保存原始文本
        original_file = os.path.join(debug_dir, f"debug_original_{timestamp}.txt")
        with open(original_file, 'w', encoding='utf-8') as f:
            for i, block in enumerate(blocks):
                f.write(f"{i+1}: {block.get('original_text', '').strip()}\n")

        # 保存发送给API的文本
        to_translate_file = os.path.join(debug_dir, f"debug_to_translate_{timestamp}.txt")
        with open(to_translate_file, 'w', encoding='utf-8') as f:
            for i, block in enumerate(blocks):
                f.write(f"{i+1}: {block.get('original_text', '').strip()}\n")

        # 保存翻译结果
        translated_file = os.path.join(debug_dir, f"debug_translated_{timestamp}.txt")
        with open(translated_file, 'w', encoding='utf-8') as f:
            for i, translation in enumerate(translations):
                f.write(f"{i+1}: {translation}\n")

        # 保存对比结果
        comparison_file = os.path.join(debug_dir, f"debug_comparison_{timestamp}.txt")
        with open(comparison_file, 'w', encoding='utf-8') as f:
            f.write("序号 | 原文 | 译文\n")
            f.write("-" * 80 + "\n")
            for i, (block, translation) in enumerate(zip(blocks, translations)):
                original = block.get('original_text', '').strip()
                f.write(f"{i+1:3d} | {original[:30]:30s} | {translation[:30]:30s}\n")

        print(f"调试文件已保存到: {debug_dir}")

    def get_statistics(self, blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        获取字幕文件统计信息

        Args:
            blocks (List[Dict[str, Any]]): 字幕块列表

        Returns:
            Dict[str, Any]: 统计信息
        """
        if not blocks:
            return {}

        total_blocks = len(blocks)
        total_chars = sum(len(block.get('original_text', '')) for block in blocks)
        total_words = sum(len(block.get('original_text', '').split()) for block in blocks)

        # 计算时长
        if len(blocks) > 1:
            start_time = self._timestamp_to_milliseconds(blocks[0].get('start_time', '00:00:00,000'))
            end_time = self._timestamp_to_milliseconds(blocks[-1].get('end_time', '00:00:00,000'))
            duration_seconds = (end_time - start_time) / 1000
        else:
            duration_seconds = 0

        # 平均每条字幕的信息
        avg_chars_per_block = total_chars / total_blocks if total_blocks > 0 else 0
        avg_words_per_block = total_words / total_blocks if total_blocks > 0 else 0

        return {
            'total_blocks': total_blocks,
            'total_characters': total_chars,
            'total_words': total_words,
            'duration_seconds': duration_seconds,
            'duration_formatted': self._format_duration(duration_seconds),
            'avg_characters_per_block': round(avg_chars_per_block, 1),
            'avg_words_per_block': round(avg_words_per_block, 1)
        }

    def _format_duration(self, seconds: float) -> str:
        """格式化时长显示"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"


# 测试代码
if __name__ == "__main__":
    # 创建测试SRT内容
    test_srt = """1
00:00:01,000 --> 00:00:03,000
这是第一段字幕

2
00:00:04,000 --> 00:00:06,000
这是第二段字幕

3
00:00:07,000 --> 00:00:09,000
这是第三段字幕
"""

    parser = SRTParser()

    try:
        # 测试解析
        blocks = parser.parse(test_srt)
        print(f"解析成功，共{len(blocks)}个字幕块")

        # 测试验证
        is_valid, errors = parser.validate_srt_format(test_srt)
        print(f"格式验证: {'通过' if is_valid else '失败'}")
        if errors:
            print("错误信息:", errors)

        # 测试统计
        stats = parser.get_statistics(blocks)
        print("统计信息:", stats)

        # 测试重建
        rebuilt_content = parser.rebuild(blocks)
        print("重建成功")

    except Exception as e:
        print(f"测试失败: {str(e)}")

