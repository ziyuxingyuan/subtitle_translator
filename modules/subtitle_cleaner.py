#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Subtitle cleaning and formatting utilities.
Recovered from historical local snapshot and trimmed to core processor logic.
"""

import sys
import os
import re
import json
import difflib
import bisect


# ==============================================================================
# 【用户可改】对齐拆短参数（影响 *_fixed.srt 的分段与时间切法）
# 改完保存脚本即可生效；数值越“保守”，拆出来的行数越少。
# ----------------------------------------------------------------------------
# 目标：字幕平均 3-5 秒、单条不超过 10 秒；并且能自动拆开“跨场景/跨说话”的离谱合并（例如 200+ 秒）。
#
# 1) 目标时长（秒）：只有在“必须切”的长句里，会尽量挑靠近这个值的切点（不会强行每条都切到这个值）。
CFG_ALIGN_TARGET_SECONDS = 4.0
# 2) 软上限（秒）：一条字幕超过这个时长时，会更积极地寻找自然断点（标点/停顿）去切短。
CFG_ALIGN_SOFT_MAX_SECONDS = 6.0
# 3) 硬上限（秒）：单条字幕绝不超过这个时长（超过一定会切）。
CFG_ALIGN_MAX_DURATION_SECONDS = 10.0
# 4) 最短时长（秒）：太短会尽量并回上一条（避免 0.x 秒碎字幕）。
CFG_ALIGN_MIN_DURATION_SECONDS = 1.2
# 5) 停顿阈值（秒）：基于单字时间戳检测“停顿”。
#    - 软停顿：可作为自然断点（避免切太碎用得更谨慎）。
CFG_ALIGN_PAUSE_SOFT_SPLIT_SEC = 1.5
#    - 硬停顿：只要出现就强制切（专门解决 200+ 秒离谱合并 / 跨场景粘连）。
CFG_ALIGN_PAUSE_HARD_SPLIT_SEC = 2.0
#    - 超级停顿：保险阈值（一般保持 5 秒即可）。
CFG_ALIGN_PAUSE_SUPER_SPLIT_SEC = 5.0
# 6) （可选）单条字幕最大/最小字符数（近似，按日文 token 拼接长度算）。
CFG_ALIGN_MAX_CHARS_PER_BLOCK = 60
CFG_ALIGN_MIN_CHARS_PER_BLOCK = 4


# 8) 【对齐匹配】时间窗口（秒）：对齐 pre.srt 文本时，只在该字幕时间前后各 N 秒范围内搜索。
CFG_ALIGN_TIME_SEARCH_WINDOW_SEC = 10.0
# 9) 【对齐匹配】若在时间窗口内文本匹配失败，是否回退到“纯时间映射”以避免丢行。
#    True: 不丢字幕行（但极少数行可能出现文本与时间不完美贴合）；False: 严格匹配，失败则该行对齐为 None。
CFG_ALIGN_FALLBACK_TO_TIME_ONLY = True
# 7) 【可选】时间戳异常修复（建议保持开启）
#    说明：有时 Whisper/降噪后会出现“单个词持续十几秒”的异常时间戳，或“字幕占了很久但字很少”的情况。
#    下面这些规则只会在“明显不合理”时生效：只缩短 end，不会把后面的字幕整体前移，避免连锁错位。

# (A) 单个词的最大持续时间（秒）。超过就认为是异常，把 end 截断到 start + MAX_WORD_SECONDS（并尽量不越过下一个词的 start）。
CFG_REPAIR_MAX_WORD_SECONDS = 1.5
# (A) 截断时给下一个词预留的安全间隔（秒）
CFG_REPAIR_WORD_END_PAD_SEC = 0.05

# (B) 字幕“字符密度”异常修复：当一条字幕持续很久但字很少时，提前结束它（只改 end）
#     - 长时长阈值：超过这个时长才会用 cps 检查（避免误伤正常 2~3 秒短句）
CFG_REPAIR_LONG_DURATION_SEC = 6.0
#     - 触发阈值：每秒字符数(cps) 低于这个值认为不合理（例如 10 秒只有 2~4 个字）
CFG_REPAIR_CPS_MIN = 1.0
#     - 目标阅读速度：用 chars / TARGET_CPS 估算更合理的显示时长（只在修复时用）
CFG_REPAIR_TARGET_CPS = 5.0
#     - 修复后时长的最小/最大限制（秒）
CFG_REPAIR_COMPRESS_MIN_SEC = 0.8
CFG_REPAIR_COMPRESS_MAX_SEC = 3.0
#     - 极短文本特殊规则：字符数 <= N 且时长 > M 秒时，也会触发修复（比如“嗯”“是”“。”）
CFG_REPAIR_SHORT_TEXT_CHARS = 3
CFG_REPAIR_SHORT_TEXT_MAX_SEC = 2.5
# --------------------------------------------------------
# 【显示时长修复（可读性）】
# 1) 触发阈值：当某条字幕原始时长 < 这个值（秒）时，才尝试延长显示时间
# 2) 目标最小时长：尽量把短字幕延长到这个时长（秒）
# 3) 安全间隔：与前后字幕至少保留这么多毫秒，确保完全不重叠
CFG_DISPLAY_EXTEND_TRIGGER_SECONDS = 1.0
CFG_DISPLAY_MIN_SECONDS = 1.5
CFG_DISPLAY_PAD_MS = 50

# ==============================================================================


# ==============================================================================
# 核心处理逻辑 (v10.0 - JSON 支持 & Whisper 格式化版)
# ==============================================================================

class SubtitleProcessor:
    # --- 格式化配置 ---
    # 【样式】单行最大字数 (推荐值: 25-35)
    MAX_LINE_WIDTH = 30
    # 【样式】单个字幕块的最大行数 (推荐值: 2)
    MAX_LINE_COUNT = 2
    # 【样式】单个字幕块的最大显示时长 (单位: 秒, 推荐值: 10)
    MAX_DURATION_SECONDS = 10
    # 【合并】触发“短句合并”功能的最大时间间隔 (单位: 毫秒, 推荐值: 300-800)
    MERGE_MAX_GAP_MS = 500
    # 【断句】用于“智能标点断句”的标点符号集合
    PUNCTUATION_SPLIT_CHARS = "。！？，、,!?."
    # --- 对齐匹配（v10.6.6+）---
    # 仅在该字幕时间前后各 N 秒范围内搜索，避免重复短句错配
    ALIGN_TIME_SEARCH_WINDOW_SEC = CFG_ALIGN_TIME_SEARCH_WINDOW_SEC
    # 时间窗口内匹配失败时，是否回退到纯时间映射以避免丢行
    ALIGN_FALLBACK_TO_TIME_ONLY = CFG_ALIGN_FALLBACK_TO_TIME_ONLY

    # 【断句】触发“智能标点断句”的最小句子长度 (推荐值: 30-40)
    PUNCTUATION_SPLIT_MIN_LEN = 30
    
    # 【v8.5 新增】定义“短句(A)”的阈值
    MERGE_SHORT_SENTENCE_THRESHOLD = 5
    # 【v8.5 新增】合并后的“聚合句(C)”的最大长度
    MERGE_AGGREGATED_MAX_CHARS = 15


    # --- 噪音识别配置 (内部使用) ---
    ANY_KANA_CHARS = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをんっアイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲンッがぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽガギグゲゴザジズゼゾダヂヅデドバビブベボパピプペポ"
    # 【最优解】
    # 仅包含：元音(a/i/u/e/o)、h行(ha/hi...用于笑声)、n(嗯)、促音/长音
    # 同时包含平假名和片假名（Whisper常输出片假名噪音）
    STRICT_NOISE_CHARS = "あいうえおっはひふへほんうーアイウエオッハヒフヘホンウー"
    PUNCTUATION = "。、.,!?？！「」"
    WHITESPACE = " \t\n\r"
    
    STRICT_NOISE_REGEX = re.compile(
        f"^[{re.escape(STRICT_NOISE_CHARS + PUNCTUATION + WHITESPACE)}]+$"
    )
    ANY_KANA_REGEX = re.compile(
        f"^[{re.escape(ANY_KANA_CHARS + PUNCTUATION + WHITESPACE)}]+$"
    )
    REPETITIVE_SOUND_REGEX = re.compile(
        r"^(?P<char>[あいうえおんうーん])"
        r"([\s、。ーっ]*(?P=char))*"
        r"[\s、。ーっ!?？！]*$"
    , re.IGNORECASE)

    REPETITIVE_CHARS_SET = "あいうえおんうーん"
    
    LEADING_REPETITIVE_SOUND_REGEX = re.compile(
        r"^(?P<char>[{set}])"
        r"([\s、。ーっ]*(?P=char))+"
        r"[\s、。ーっ!?？！]*".format(set=REPETITIVE_CHARS_SET)
    )

    TRAILING_REPETITIVE_SOUND_REGEX = re.compile(
        r"[\s、。ーっ!?？！]*"
        r"(?P<char>[{set}])"
        r"([\s、。ーっ]*(?P=char))+$".format(set=REPETITIVE_CHARS_SET)
    )

    ELLIPSIS_REGEX = re.compile(r"[…\.．]{2,}")
    PARENTHESIZED_TEXT_REGEX = re.compile(r'\(.*?\)|（.*?）|［.*?］')
    OVERLAP_NOISE_REGEX = re.compile(
        f"^[{re.escape(PUNCTUATION + WHITESPACE)}]*"
        f"[{re.escape(ANY_KANA_CHARS)}]"
        f"([{re.escape(ANY_KANA_CHARS)}])?"
        f"[{re.escape(PUNCTUATION + WHITESPACE)}]*$"
    )
    HAS_KANJI_REGEX = re.compile(r'[一-龯]')
    MAX_CONSECUTIVE_GAP_MS = 2000
    KANA_WHITELIST = {
        "はい", "いいえ",
        "ごめん", "ごめんね", "すみません",
        "ありがとう", "ありがと", "どうも", "サンキュー",
        "ね", "ねぇ", "あの", "あのね",
        "そっか", "そう", "そうね", "そうだ",
        "えっ", "あれ", "まさか", "うそ", "マジ", "まじ",
        "ちょっと", "まって",
        "おはよう", "おやすみ", "おかえり", "ただいま",
        "もしもし", "なるほど",
        "うん", "ううん", "ええ", "うんうん",
        "ホテル", "ラブホ", "ベッド", "ゴム", "オイル", "パンツ", "ブラ",
        "コスプレ", "メイド",
    }

    # ======================================================================
    # 一键拆短（仅 JSON）：word/token 粒度去呻吟/噪音
    # ======================================================================
    ONECLICK_DROP_MOAN_ENABLED = True
    ONECLICK_MOAN_MIN_DUR_SEC = 0.35
    ONECLICK_TINY_MERGE_GAP_MS = 250  # ≤此间隔(ms)的极短噪音片段可并回上一条；否则直接丢弃

    ONECLICK_SMALL_KANA = "ぁぃぅぇぉゃゅょっァィゥェォャュョッ"
    ONECLICK_MOAN_ONLY_REGEX = re.compile(rf"^[{ONECLICK_SMALL_KANA}ー〜～…]+$")
    ONECLICK_MOAN_VOWEL_REGEX = re.compile(r"^(?:[あぁうぅおぉはふんン]+[ぁぃぅぇぉー〜～っ…]*)$")
    ONECLICK_MOAN_REPEAT_REGEX = re.compile(r"^(?:あ{2,}|う{2,}|お{2,}|ん{2,}|ン{2,})$")
    ONECLICK_CN_MOAN_REGEX = re.compile(r"^(?:[啊嗯呃唔哼]+)$")
    ONECLICK_ACK_REPEAT_LINE_REGEX = re.compile(
        r"^(?P<t>あはい|あうん|うんあ|うん|はい|はあ)(?:(?P=t))*$"
    )
    ONECLICK_ACK_EXACT_SINGLE_NOISE = {
        "あー", "え", "えうん", "うんは", "はうん", "はあ",
    }
    ONECLICK_ACK_EXACT_REPEAT_NOISE_REGEX = re.compile(
        r"^(?:(?:あー){2,}|(?:え){2,}|(?:えうん){2,}|(?:うんは){2,}|(?:はうん){2,}|(?:はあ){2,})$"
    )
    ONECLICK_ACK_COMPOUND_VOCAB = (
        "あはい", "あうん", "うんあ", "うん", "はい", "はあ",
        "あー", "え", "えうん", "うんは", "はうん",
    )
    ONECLICK_ACK_PUNCT_REGEX = re.compile(r"[。、，！？…・!?,.．:：;；\s　]+")
    POST_CLEAN_PUNCT_REGEX = re.compile(r"[。、，！？…・!?,.．:：;；~～\s　]+")
    POST_CLEAN_COMPOUND_VOCAB = (
        "呜呼呼呼", "呜呼呼", "呜呼", "呼哈哈", "呼哈", "哈呼", "呼呼", "呵呵", "哼哼",
        "啊哈", "哈啊", "嗯哼", "唔嗯", "唔啊",
        "嗯呢", "嗯啊", "嗯哈",
        "啊", "阿", "嗯", "哈", "呀", "哦", "喔", "噢", "呃", "唔", "呐", "诶", "欸", "是", "哇", "呼", "呵", "哼", "嘿", "嗷", "呜",
    )


    class SubtitleBlock:
        def __init__(self, index, start_ms, end_ms, text, words=None):
            self.index, self.start_ms, self.end_ms, self.text = (
                index,
                start_ms,
                end_ms,
                text.strip(),
            )
            # 可选：词级时间戳列表，元素形如 {"word": str, "start": float, "end": float}
            self.words = words or []

        @property
        def duration(self):
            return self.end_ms - self.start_ms
            
        # 【v10.0 新增】方便转换为秒 (float) 用于 JSON 输出
        @property
        def start_sec(self):
            return round(self.start_ms / 1000.0, 3)
        
        @property
        def end_sec(self):
            return round(self.end_ms / 1000.0, 3)

        def __repr__(self):
            return f"<{self.index}|{SubtitleProcessor.ms_to_time(self.start_ms)}-->{SubtitleProcessor.ms_to_time(self.end_ms)}|{self.text}>"

    @staticmethod
    def time_to_ms(t_str):
        try:
            m_obj = re.match(r"^(?P<h>\d+):(?P<m>\d+):(?P<s>\d+(?:[.,]\d+)?)$", t_str.strip())
            if not m_obj:
                return 0
            h = int(m_obj.group("h"))
            m_val = int(m_obj.group("m"))
            s_val = float(m_obj.group("s").replace(",", "."))
            return int((h * 3600 + m_val * 60 + s_val) * 1000)
        except:
            return 0

    @staticmethod
    def ms_to_time(ms):
        ms = max(0, int(ms))
        h = ms // 3600000
        ms %= 3600000
        m = ms // 60000
        ms %= 60000
        s = ms // 1000
        ms %= 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    # --- 解析逻辑 ---

    def parse_srt(self, content):
        blocks = []
        for i, block_text in enumerate(content.strip().split("\n\n"), 1):
            lines = block_text.strip().split("\n")
            if len(lines) >= 2 and "-->" in lines[1]:
                try:
                    start_str, end_str = lines[1].split(" --> ")
                    blocks.append(
                        self.SubtitleBlock(
                            i,
                            self.time_to_ms(start_str),
                            self.time_to_ms(end_str),
                            "\n".join(lines[2:]),
                        )
                    )
                except:
                    continue
        return blocks

    def parse_json(self, content):
        """【v10.0 新增】解析 JSON 内容，尝试兼容 Whisper 格式、列表格式等"""
        blocks = []
        raw_segments = []
        data = None

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = None

        # 1. 尝试从 'segments' 键获取 (Whisper 标准)
        if isinstance(data, dict):
            if "segments" in data:
                raw_segments = data["segments"]
            elif "words" in data and isinstance(data["words"], list):
                 # 某些格式直接只有 words，尝试将其视为片段
                raw_segments = data["words"]
        # 2. 尝试直接是列表结构
        elif isinstance(data, list):
            raw_segments = data

        # 3. 兼容 "时间轴 --> 时间轴 : {json}" 的逐行格式
        if not raw_segments:
            line_pattern = re.compile(
                r"^\s*(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})\s*:?\s*(?P<payload>\{.*\})\s*$"
            )
            for line in content.splitlines():
                line = line.strip()
                if not line or "-->" not in line or "{" not in line:
                    continue
                m = line_pattern.match(line)
                if not m:
                    continue
                try:
                    payload = json.loads(m.group("payload"))
                except json.JSONDecodeError:
                    continue
                raw_segments.append(
                    {
                        "start": self.time_to_ms(m.group("start")) / 1000,
                        "end": self.time_to_ms(m.group("end")) / 1000,
                        "text": payload.get("text", ""),
                        "timestamps": payload.get("timestamps", []),
                        "tokens": payload.get("tokens", []),
                    }
                )
        
        for i, seg in enumerate(raw_segments, 1):
            # 提取开始和结束时间 (秒转毫秒)
            start = seg.get("start", 0)
            end = seg.get("end", 0)
            # 提取文本 (优先 text, 其次 word, 其次 content)
            text = seg.get("text", seg.get("word", seg.get("content", "")))
            words_data = []

            # 如果有 tokens + timestamps，尝试构造词级时间戳
            tokens_list = seg.get("tokens") or []
            ts_list = seg.get("timestamps") or []
            if tokens_list and ts_list and len(tokens_list) == len(ts_list):
                start_base = float(start) if start is not None else 0.0
                end_fallback = float(end) if end is not None else (
                    float(ts_list[-1]) if ts_list else 0.0
                )
                for idx, tok in enumerate(tokens_list):
                    try:
                        word_text = str(tok).strip()
                        if not word_text:
                            continue
                        w_start = start_base + float(ts_list[idx])
                        w_end_raw = (
                            start_base + float(ts_list[idx + 1])
                            if idx + 1 < len(ts_list)
                            else end_fallback
                        )
                        w_end = w_end_raw if w_end_raw >= w_start else w_start
                        words_data.append(
                            {
                                "word": word_text,
                                "start": round(w_start, 3),
                                "end": round(w_end, 3),
                            }
                        )
                    except Exception:
                        continue
            
            # 如果 seg 自带 words（Whisper 标准），直接使用（绝对时间：秒）
            if not words_data:
                ws = seg.get("words") or []
                if isinstance(ws, list):
                    for w in ws:
                        if not isinstance(w, dict):
                            continue
                        wt = str(w.get("word", w.get("text", ""))).strip()
                        if not wt:
                            continue
                        try:
                            ws_s = float(w.get("start", 0.0))
                            ws_e = float(w.get("end", ws_s))
                        except Exception:
                            continue
                        if ws_e < ws_s:
                            ws_e = ws_s
                        words_data.append(
                            {"word": wt, "start": round(ws_s, 3), "end": round(ws_e, 3)}
                        )

            if text:
                start_ms = int(float(start) * 1000)
                end_ms = int(float(end) * 1000)
                blocks.append(self.SubtitleBlock(i, start_ms, end_ms, str(text), words=words_data))
                
        return blocks

    def clean_blocks(self, blocks):
        cleaned_blocks = []
        for block in blocks:
            text = block.text.replace('\n', ' ')
            text = self.PARENTHESIZED_TEXT_REGEX.sub('', text)
            text = ' '.join(text.split())

            if not text:
                continue
            
            text = self.LEADING_REPETITIVE_SOUND_REGEX.sub('', text).strip()
            text = self.TRAILING_REPETITIVE_SOUND_REGEX.sub('', text).strip()
            
            if not text:
                continue

            normalized = text.strip(self.PUNCTUATION + self.WHITESPACE)
            is_kana_only = bool(self.ANY_KANA_REGEX.match(normalized))
            is_kana_whitelist = normalized in self.KANA_WHITELIST

            is_noise = False
            if self.REPETITIVE_SOUND_REGEX.match(text):
                is_noise = True
            elif self.STRICT_NOISE_REGEX.match(text) and not is_kana_whitelist:
                is_noise = True
            elif len(normalized) <= 2 and is_kana_only and not is_kana_whitelist:
                is_noise = True
            
            if not is_noise:
                block.text = text
                cleaned_blocks.append(block)
                
        return cleaned_blocks

    def clean_overlapping_noise(self, blocks):
        if len(blocks) < 2:
            return blocks

        while True:
            to_delete = set()
            i = 0
            while i < len(blocks) - 1:
                block_a = blocks[i]
                block_b = blocks[i+1]

                if block_b.start_ms < block_a.end_ms:
                    a_is_noise = self.OVERLAP_NOISE_REGEX.match(block_a.text) and not self.HAS_KANJI_REGEX.search(block_a.text)
                    b_is_noise = self.OVERLAP_NOISE_REGEX.match(block_b.text) and not self.HAS_KANJI_REGEX.search(block_b.text)
                    
                    if a_is_noise and not b_is_noise:
                        to_delete.add(i)
                        i += 1
                        continue
                    elif b_is_noise and not a_is_noise:
                        to_delete.add(i + 1)
                    elif block_a.text == block_b.text:
                        if block_a.duration >= block_b.duration:
                            to_delete.add(i + 1)
                        else:
                            to_delete.add(i)
                            i += 1
                            continue
                i += 1

            if not to_delete:
                break

            blocks = [block for i, block in enumerate(blocks) if i not in to_delete]
            
        return blocks

    def merge_consecutive_duplicates(self, blocks):
        if not blocks:
            return []

        merged_blocks = []
        i = 0
        while i < len(blocks):
            current_block = blocks[i]
            
            j = i + 1
            while j < len(blocks):
                next_block = blocks[j]
                if next_block.text == current_block.text and \
                   (next_block.start_ms - current_block.end_ms) <= self.MAX_CONSECUTIVE_GAP_MS:
                    current_block.end_ms = max(current_block.end_ms, next_block.end_ms)
                    j += 1
                else:
                    break
            
            merged_blocks.append(current_block)
            i = j
            
        return merged_blocks

    def format_blocks(self, blocks, punc_split_enabled, ellipses_mode, merge_short_enabled):
        filtered_blocks = []
        for block in blocks:
            text = block.text.strip()
            if not self.ELLIPSIS_REGEX.fullmatch(text):
                filtered_blocks.append(block)
        blocks = filtered_blocks

        if ellipses_mode != "none":
            for block in blocks:
                text = block.text.strip()
                if ellipses_mode == "replace":
                    text = self.ELLIPSIS_REGEX.sub(
                        lambda m: "。" if m.end() == len(text) else "，", text
                    )
                elif ellipses_mode == "normalize":
                    text = self.ELLIPSIS_REGEX.sub("……", text)
                block.text = text

        if punc_split_enabled:
            pre_processed_blocks = []
            for block in blocks:
                text = block.text.replace("\n", " ").strip()
                text = re.sub(f"([{self.PUNCTUATION_SPLIT_CHARS}]) +", r"\1", text)
                if len(text) < self.PUNCTUATION_SPLIT_MIN_LEN:
                    pre_processed_blocks.append(block)
                    continue
                split_indices = [0] + [
                    m.end() for m in re.finditer(f"[{self.PUNCTUATION_SPLIT_CHARS}]", text)
                ]
                if len(split_indices) <= 1:
                    pre_processed_blocks.append(block)
                    continue
                sub_texts = [
                    text[split_indices[i] : split_indices[i + 1]].strip()
                    for i in range(len(split_indices) - 1)
                ]
                sub_texts.append(text[split_indices[-1] :].strip())
                sub_texts = [s for s in sub_texts if s]
                total_len = len(text)
                time_cursor = block.start_ms
                for sub_text in sub_texts:
                    sub_len = len(sub_text)
                    duration = (
                        int(block.duration * (sub_len / total_len)) if total_len > 0 else 0
                    )
                    pre_processed_blocks.append(
                        self.SubtitleBlock(0, time_cursor, time_cursor + duration, sub_text)
                    )
                    time_cursor += duration
            blocks = pre_processed_blocks

        # 短句合并逻辑
        if merge_short_enabled:
            merged = []
            i = 0
            while i < len(blocks):
                current_block = blocks[i]
                
                if len(current_block.text) <= self.MERGE_SHORT_SENTENCE_THRESHOLD:
                    merge_group = [current_block]
                    merged_text = current_block.text
                    last_end_ms = current_block.end_ms
                    
                    j = i + 1
                    while j < len(blocks):
                        next_block = blocks[j]
                        
                        is_short_sentence = len(next_block.text) <= self.MERGE_SHORT_SENTENCE_THRESHOLD
                        is_gap_ok = (next_block.start_ms - last_end_ms) <= self.MERGE_MAX_GAP_MS
                        is_len_ok = len(merged_text) + 1 + len(next_block.text) <= self.MERGE_AGGREGATED_MAX_CHARS
                        
                        if is_short_sentence and is_gap_ok and is_len_ok:
                            merge_group.append(next_block)
                            merged_text += " " + next_block.text
                            last_end_ms = next_block.end_ms
                            j += 1
                        else:
                            break
                    
                    if len(merge_group) > 1:
                        new_block = self.SubtitleBlock(
                            current_block.index,
                            current_block.start_ms,
                            last_end_ms,
                            merged_text
                        )
                        merged.append(new_block)
                        i = j
                    else:
                        merged.append(current_block)
                        i += 1
                else:
                    merged.append(current_block)
                    i += 1
            blocks = merged

        final_blocks = []
        for block in blocks:
            source_text = block.text.replace("\n", " ").strip()
            if not source_text:
                continue
            lines = []
            temp_text = source_text
            while len(temp_text) > 0:
                if len(temp_text) <= self.MAX_LINE_WIDTH:
                    lines.append(temp_text)
                    break
                cut_pos = -1
                for i in range(self.MAX_LINE_WIDTH, 0, -1):
                    if i < len(temp_text) and temp_text[i] in "。、, ":
                        cut_pos = i + 1
                        break
                if cut_pos == -1:
                    cut_pos = self.MAX_LINE_WIDTH
                lines.append(temp_text[:cut_pos].strip())
                temp_text = temp_text[cut_pos:].strip()
            chunks = [
                "\n".join(lines[i : i + self.MAX_LINE_COUNT])
                for i in range(0, len(lines), self.MAX_LINE_COUNT)
            ]
            total_chars = len(source_text)
            time_cursor = block.start_ms
            for chunk_text in chunks:
                chunk_chars = len(chunk_text.replace("\n", ""))
                prop_dur = (
                    int(block.duration * (chunk_chars / total_chars))
                    if total_chars > 0
                    else block.duration
                )
                final_dur = min(prop_dur, self.MAX_DURATION_SECONDS * 1000)
                final_blocks.append(
                    self.SubtitleBlock(0, time_cursor, time_cursor + final_dur, chunk_text)
                )
                time_cursor += prop_dur

        return final_blocks

    def generate_whisper_json(self, blocks):
        """【v10.0 新增】将处理后的块转换为 Whisper 格式的 JSON"""
        segments = []
        full_text_parts = []
        
        for i, block in enumerate(blocks):
            # 清理换行符，因为JSON一般存单行文本
            clean_text = block.text.replace("\n", " ")
            full_text_parts.append(clean_text)
            block_words = block.words or []
            
            segments.append({
                "id": i,
                "seek": 0, # 简化处理
                "start": block.start_sec,
                "end": block.end_sec,
                "text": clean_text,
                "tokens": [], # 不生成 tokens
                "words": block_words,
                "temperature": 0.0,
                "avg_logprob": 0.0,
                "compression_ratio": 0.0,
                "no_speech_prob": 0.0
            })
        
        # 汇总顶层 words，便于下游直接使用
        top_words = []
        for seg in segments:
            for w in seg.get("words", []):
                top_words.append(w)
            
        output_data = {
            "text": " ".join(full_text_parts),
            "segments": segments,
            "words": top_words,
            "language": "ja" # 假设是日语，可根据需要修改
        }
        return output_data

    # ==============================================================================
    # v10.0 - 词级时间轴对齐 / 拆短（避免“按字数比例分时间”导致漂移）
    # ==============================================================================
    # 【拆短】目标时长（秒）：只在“需要切”的长句里用来挑切点（不会强行每条都切到这个值）
    ALIGN_TARGET_SECONDS = CFG_ALIGN_TARGET_SECONDS
    # 【拆短】软上限（秒）：超过就更积极找自然断点切短
    ALIGN_SOFT_MAX_SECONDS = CFG_ALIGN_SOFT_MAX_SECONDS
    # 【拆短】硬上限（秒）：单条字幕绝不超过这个值（超过一定切）
    ALIGN_MAX_DURATION_SECONDS = CFG_ALIGN_MAX_DURATION_SECONDS
    # 【拆短】单条字幕最短可读时长（秒）：太短会尽量并回上一条
    ALIGN_MIN_DURATION_SECONDS = CFG_ALIGN_MIN_DURATION_SECONDS
    # 【拆短】停顿阈值（秒）：基于 words 的时间戳判断
    ALIGN_PAUSE_SOFT_SPLIT_SEC = CFG_ALIGN_PAUSE_SOFT_SPLIT_SEC
    ALIGN_PAUSE_HARD_SPLIT_SEC = CFG_ALIGN_PAUSE_HARD_SPLIT_SEC
    ALIGN_PAUSE_SUPER_SPLIT_SEC = CFG_ALIGN_PAUSE_SUPER_SPLIT_SEC
    # 【拆短】单条字幕最大/最小字符数（近似）
    ALIGN_MAX_CHARS_PER_BLOCK = CFG_ALIGN_MAX_CHARS_PER_BLOCK
    ALIGN_MIN_CHARS_PER_BLOCK = CFG_ALIGN_MIN_CHARS_PER_BLOCK

    # 【修复】时间戳异常修复参数（只在明显不合理时生效）
    REPAIR_MAX_WORD_SECONDS = CFG_REPAIR_MAX_WORD_SECONDS
    REPAIR_WORD_END_PAD_SEC = CFG_REPAIR_WORD_END_PAD_SEC
    REPAIR_LONG_DURATION_SEC = CFG_REPAIR_LONG_DURATION_SEC
    REPAIR_CPS_MIN = CFG_REPAIR_CPS_MIN
    REPAIR_TARGET_CPS = CFG_REPAIR_TARGET_CPS
    REPAIR_COMPRESS_MIN_SEC = CFG_REPAIR_COMPRESS_MIN_SEC
    REPAIR_COMPRESS_MAX_SEC = CFG_REPAIR_COMPRESS_MAX_SEC
    REPAIR_SHORT_TEXT_CHARS = CFG_REPAIR_SHORT_TEXT_CHARS
    REPAIR_SHORT_TEXT_MAX_SEC = CFG_REPAIR_SHORT_TEXT_MAX_SEC
    # 【对齐】用于识别“日文 vs 中文”的简单统计
    _KANA_REGEX = re.compile(r"[ぁ-ゖァ-ヺー]")
    _HAN_REGEX = re.compile(r"[\u4e00-\u9fff]")

    def _normalize_for_match(self, s: str) -> str:
        """用于“找回时间轴”的轻量归一化：去空白、统一常见标点。"""
        if not s:
            return ""
        s = s.replace("\n", "").replace("\r", "").replace("\t", "")
        s = re.sub(r"\s+", "", s)

        # 统一常见中/英/日标点，让 find 更稳
        trans = {
            ",": "、",
            "，": "、",
            ".": "。",
            "．": "。",
            "!": "！",
            "?": "？",
        }
        s = "".join(trans.get(ch, ch) for ch in s)

        # 省略号统一（避免 ... / …… / …）
        s = re.sub(r"(\.{2,}|…{2,}|．{2,})", "……", s)
        return s

    def _load_whisper_words(self, json_path: str):
        """读取词级时间戳（秒），兼容多种输入格式。"""

        def _normalize_word_obj(obj: dict) -> dict | None:
            if not isinstance(obj, dict):
                return None
            w = obj.get("word")
            if w is None:
                w = obj.get("text")
            if w is None:
                return None
            try:
                s = float(obj.get("start", 0.0))
                e = float(obj.get("end", s))
            except Exception:
                return None
            if e < s:
                e = s
            result = {"word": str(w), "start": round(s, 3), "end": round(e, 3)}
            display_word = obj.get("display_word")
            if isinstance(display_word, str) and display_word.strip():
                result["display_word"] = display_word
            return result

        def _build_words_from_tokens(tokens: list, timestamps: list, seg_start_sec: float, seg_end_sec: float) -> list:
            """把 tokens + timestamps（相对段起点秒）转成 words[{word,start,end}]（绝对秒）。"""
            out = []
            if not isinstance(tokens, list) or not isinstance(timestamps, list):
                return out
            if not tokens or not timestamps:
                return out

            tok = [str(x) for x in tokens]
            ts: list[float | None] = []
            for x in timestamps:
                try:
                    ts.append(float(x))
                except Exception:
                    ts.append(None)

            if len(ts) == len(tok) + 1:
                start_offsets = ts[:-1]
                end_boundary = ts[-1]
                n = min(len(tok), len(start_offsets))
                for i in range(n):
                    t_i = tok[i]
                    if not t_i or not t_i.strip():
                        continue
                    s_off = start_offsets[i]
                    if s_off is None:
                        continue
                    if i + 1 < n and start_offsets[i + 1] is not None:
                        e_off = start_offsets[i + 1]
                    else:
                        e_off = end_boundary if end_boundary is not None else (seg_end_sec - seg_start_sec)
                    s = seg_start_sec + float(s_off)
                    e = seg_start_sec + float(e_off)
                    if e > seg_end_sec:
                        e = seg_end_sec
                    if e < s:
                        e = min(seg_end_sec, s + 0.05)
                    out.append({"word": t_i, "start": round(s, 3), "end": round(e, 3)})
                return out

            n = min(len(tok), len(ts))
            for i in range(n):
                t_i = tok[i]
                if not t_i or not t_i.strip():
                    continue
                s_off = ts[i]
                if s_off is None:
                    continue
                if i + 1 < n and ts[i + 1] is not None:
                    e_off = ts[i + 1]
                else:
                    e_off = seg_end_sec - seg_start_sec
                s = seg_start_sec + float(s_off)
                e = seg_start_sec + float(e_off)
                if e > seg_end_sec:
                    e = seg_end_sec
                if e < s:
                    e = min(seg_end_sec, s + 0.05)
                out.append({"word": t_i, "start": round(s, 3), "end": round(e, 3)})
            return out

        def _load_from_whisper_json(data) -> list:
            words = []

            if isinstance(data, dict):
                top = data.get("words")
                if isinstance(top, list) and top:
                    for w in top:
                        nw = _normalize_word_obj(w)
                        if nw:
                            words.append(nw)
                    if words:
                        return words

                segs = data.get("segments") or []
                if isinstance(segs, list):
                    for seg in segs:
                        if not isinstance(seg, dict):
                            continue
                        ws = seg.get("words")
                        if isinstance(ws, list) and ws:
                            for w in ws:
                                nw = _normalize_word_obj(w)
                                if nw:
                                    words.append(nw)

                if not words and isinstance(segs, list) and segs:
                    for seg in segs:
                        if not isinstance(seg, dict):
                            continue
                        try:
                            seg_start = float(seg.get("start", 0.0))
                            seg_end = float(seg.get("end", seg_start))
                        except Exception:
                            continue
                        tokens = seg.get("tokens") or []
                        ts = seg.get("timestamps") or []
                        words.extend(_build_words_from_tokens(tokens, ts, seg_start, seg_end))
                return words

            if isinstance(data, list):
                if data and isinstance(data[0], dict) and ("start" in data[0]) and ("end" in data[0]):
                    for w in data:
                        nw = _normalize_word_obj(w)
                        if nw:
                            words.append(nw)
                    return words

                for seg in data:
                    if not isinstance(seg, dict):
                        continue
                    ws = seg.get("words")
                    if isinstance(ws, list) and ws:
                        for w in ws:
                            nw = _normalize_word_obj(w)
                            if nw:
                                words.append(nw)
                    else:
                        try:
                            seg_start = float(seg.get("start", 0.0))
                            seg_end = float(seg.get("end", seg_start))
                        except Exception:
                            continue
                        tokens = seg.get("tokens") or []
                        ts = seg.get("timestamps") or []
                        words.extend(_build_words_from_tokens(tokens, ts, seg_start, seg_end))
                return words

            return []

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            words = _load_from_whisper_json(data)
            return words
        except Exception:
            with open(json_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()

            header_pat = re.compile(
                r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})\s*:\s*",
                re.M,
            )
            matches = list(header_pat.finditer(raw))
            if not matches:
                return []

            out_words = []
            for idx, m in enumerate(matches):
                seg_start_str = m.group("start")
                seg_end_str = m.group("end")
                seg_start_sec = self.time_to_ms(seg_start_str) / 1000.0
                seg_end_sec = self.time_to_ms(seg_end_str) / 1000.0

                payload_start = m.end()
                payload_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
                payload = raw[payload_start:payload_end].strip()
                if not payload:
                    continue
                last_brace = payload.rfind("}")
                if last_brace == -1:
                    continue
                payload = payload[: last_brace + 1]

                try:
                    obj = json.loads(payload)
                except Exception:
                    continue

                tokens = obj.get("tokens") or []
                ts = obj.get("timestamps") or []
                ws = obj.get("words")
                if isinstance(ws, list) and ws:
                    for w in ws:
                        if not isinstance(w, dict):
                            continue
                        try:
                            s = float(w.get("start", 0.0))
                            e = float(w.get("end", s))
                        except Exception:
                            continue
                        if e < s:
                            e = s
                        seg_dur = max(0.001, seg_end_sec - seg_start_sec)
                        if e <= seg_dur + 0.01:
                            s += seg_start_sec
                            e += seg_start_sec
                        out_words.append(
                            {
                                "word": str(w.get("word", w.get("text", ""))),
                                "start": round(s, 3),
                                "end": round(e, 3),
                            }
                        )
                    continue

                out_words.extend(_build_words_from_tokens(tokens, ts, seg_start_sec, seg_end_sec))

            return out_words

    def _repair_word_durations(self, words):
        """修复 words 时间戳异常：限制单个词的最大持续时间（只缩短 end）。"""
        try:
            max_word = float(self.REPAIR_MAX_WORD_SECONDS)
        except Exception:
            max_word = 1.5
        try:
            pad = float(self.REPAIR_WORD_END_PAD_SEC)
        except Exception:
            pad = 0.05

        if not words:
            return words

        # 就地修改：只缩短 end，不移动 start
        for idx, w in enumerate(words):
            try:
                s = float(w.get("start", 0.0))
                e = float(w.get("end", s))
            except Exception:
                continue
            if e < s:
                e = s

            # 参考下一词，避免 end 穿过下一个 start
            next_start = None
            if idx + 1 < len(words):
                try:
                    next_start = float(words[idx + 1].get("start", None))
                except Exception:
                    next_start = None

            limit_end = s + max_word
            if next_start is not None:
                limit_end = min(limit_end, max(s, next_start - pad))

            if (e - s) > max_word + 1e-6:
                e = limit_end

            # 再次保证单调
            if e < s:
                e = s

            w["start"] = round(s, 3)
            w["end"] = round(e, 3)

        return words

    # ======================================================================
    # 一键拆短（仅 JSON）：word/token 粒度去呻吟/噪音
    # ======================================================================
    def _oneclick_is_punct_only(self, t: str) -> bool:
        if not t:
            return True
        keep_chars = set(self.PUNCTUATION + "・:：;；()（）[]【】{}<>「」『』 　\t\n\r")
        return all(ch in keep_chars for ch in t)

    def _is_ack_compound_noise_after_punct_strip(self, text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        t = self.ONECLICK_ACK_PUNCT_REGEX.sub("", t)
        if not t:
            return False

        n = len(t)
        dp = [False] * (n + 1)
        dp[0] = True
        tokens = sorted(self.ONECLICK_ACK_COMPOUND_VOCAB, key=len, reverse=True)
        for i in range(n):
            if not dp[i]:
                continue
            for tok in tokens:
                if t.startswith(tok, i):
                    dp[i + len(tok)] = True
        return dp[n]

    def _is_single_line_ack_repeat_noise(self, text: str, duration_ms: int | None = None) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        if len(t.splitlines()) != 1:
            return False
        if t in self.ONECLICK_ACK_EXACT_SINGLE_NOISE:
            return True
        if self.ONECLICK_ACK_EXACT_REPEAT_NOISE_REGEX.fullmatch(t):
            return True
        if self.ONECLICK_ACK_REPEAT_LINE_REGEX.fullmatch(t) is not None:
            return True
        # duration_ms 参数保留是为了兼容现有调用链；复合噪声判定不依赖时长。
        if self._is_ack_compound_noise_after_punct_strip(t):
            return True
        return False

    def _is_post_translate_compound_noise_after_punct_strip(self, text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        t = self.POST_CLEAN_PUNCT_REGEX.sub("", t)
        if not t:
            return False

        n = len(t)
        dp = [False] * (n + 1)
        dp[0] = True
        tokens = sorted(self.POST_CLEAN_COMPOUND_VOCAB, key=len, reverse=True)
        for i in range(n):
            if not dp[i]:
                continue
            for tok in tokens:
                if t.startswith(tok, i):
                    dp[i + len(tok)] = True
        return dp[n]

    def _is_single_line_post_translation_noise(self, text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        if len(t.splitlines()) != 1:
            return False
        return self._is_post_translate_compound_noise_after_punct_strip(t)

    def _oneclick_is_noise_token(self, token: str, dur_sec: float) -> bool:
        t = (token or "").strip()
        if not t:
            return True

        if t in self.KANA_WHITELIST:
            return False
        if self.HAS_KANJI_REGEX.search(t):
            return False
        if self._oneclick_is_punct_only(t):
            return False
        if self.ONECLICK_CN_MOAN_REGEX.match(t):
            return True
        if self.ONECLICK_MOAN_ONLY_REGEX.match(t):
            return True
        if dur_sec >= float(self.ONECLICK_MOAN_MIN_DUR_SEC):
            if self.ONECLICK_MOAN_VOWEL_REGEX.match(t) or self.ONECLICK_MOAN_REPEAT_REGEX.match(t):
                return True
        if len(t) <= 2 and re.fullmatch(r"(?:[あぁうぅおぉんン]+|[ー〜～]+)(?:っ)?", t):
            return True
        return False

    def _oneclick_filter_words(self, words: list) -> list:
        if not words:
            return []
        if not bool(self.ONECLICK_DROP_MOAN_ENABLED):
            return words

        kept = []
        for w in words:
            try:
                t = str(w.get("word", "")).strip()
                s = float(w.get("start", 0.0))
                e = float(w.get("end", s))
            except Exception:
                continue
            dur = max(0.0, e - s)
            if self._oneclick_is_noise_token(t, dur):
                continue
            kept.append(w)
        return kept

    def _oneclick_merge_tiny_spans(self, spans: list, words: list) -> list:
        if not spans:
            return spans

        def span_text(a: int, b: int) -> str:
            return "".join(words[k]["word"] for k in range(a, b + 1)).strip()

        def span_start_ms(a: int) -> int:
            try:
                return int(float(words[a]["start"]) * 1000)
            except Exception:
                return 0

        def span_end_ms(b: int) -> int:
            try:
                return int(float(words[b]["end"]) * 1000)
            except Exception:
                return 0

        def is_single_kana(t: str) -> bool:
            return len(t) == 1 and re.fullmatch(r"[ぁ-ゖァ-ヺ]", t) is not None

        def is_tiny_noise_text(t: str) -> bool:
            if not t:
                return True
            if t in self.KANA_WHITELIST:
                return False
            if self._oneclick_is_punct_only(t):
                return True
            if is_single_kana(t):
                return True
            if t and t[0] in self.ONECLICK_SMALL_KANA:
                return True
            if self.ONECLICK_MOAN_ONLY_REGEX.match(t):
                return True
            if len(t) <= 2 and re.fullmatch(r"(?:[あぁうぅおぉんン]+|[ー〜～]+)(?:っ)?", t):
                return True
            return False

        merged: list[tuple[int, int]] = []
        for (a, b) in spans:
            t = span_text(a, b)

            if is_tiny_noise_text(t):
                if not merged:
                    continue

                pa, pb = merged[-1]
                gap_ms = span_start_ms(a) - span_end_ms(pb)
                if gap_ms < 0:
                    gap_ms = 0

                if gap_ms <= int(self.ONECLICK_TINY_MERGE_GAP_MS):
                    merged[-1] = (pa, b)
                else:
                    continue
            else:
                merged.append((a, b))

        return merged

    def process_json_split_oneclick(self, json_path: str) -> str:
        base, ext = os.path.splitext(json_path)
        if ext.lower() != ".json":
            raise ValueError("优化字幕仅支持 JSON 输入（*.json）。")

        def _time_to_seconds(t: str) -> float:
            t = (t or "").strip()
            if not t:
                return 0.0
            t = t.replace(",", ".")
            parts = t.split(":")
            if len(parts) != 3:
                return 0.0
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
            return h * 3600.0 + m * 60.0 + s

        header_re = re.compile(
            r"^\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*:\s*(.*)\s*$"
        )

        def _normalize_word_obj(obj: dict) -> dict | None:
            if not isinstance(obj, dict):
                return None
            w = obj.get("word")
            if w is None:
                w = obj.get("text")
            if w is None:
                return None
            try:
                s = float(obj.get("start", obj.get("s", 0.0)))
                e = float(obj.get("end", obj.get("e", s)))
            except Exception:
                return None
            result = {"word": str(w), "start": s, "end": e}
            display_word = obj.get("display_word")
            if isinstance(display_word, str) and display_word.strip():
                result["display_word"] = display_word
            return result

        def _build_words_from_tokens(tokens, timestamps, seg_start_abs: float, seg_end_abs: float) -> list:
            out = []
            if not isinstance(tokens, list) or not tokens:
                return out
            if not isinstance(timestamps, list) or not timestamps:
                dur = max(0.0, seg_end_abs - seg_start_abs)
                step = dur / max(1, len(tokens))
                for i, tok in enumerate(tokens):
                    s = seg_start_abs + i * step
                    e = seg_start_abs + (i + 1) * step
                    out.append({"word": str(tok), "start": s, "end": e})
                return out

            try:
                ts = [float(x) for x in timestamps]
            except Exception:
                ts = []

            seg_dur = max(0.0, seg_end_abs - seg_start_abs)
            if ts and len(ts) == len(tokens) + 1:
                for i, tok in enumerate(tokens):
                    s_off = max(0.0, min(seg_dur, ts[i]))
                    e_off = max(0.0, min(seg_dur, ts[i + 1]))
                    if e_off < s_off:
                        e_off = s_off
                    out.append(
                        {"word": str(tok), "start": seg_start_abs + s_off, "end": seg_start_abs + e_off}
                    )
                return out
            if ts and len(ts) == len(tokens):
                for i, tok in enumerate(tokens):
                    s_off = max(0.0, min(seg_dur, ts[i]))
                    if i + 1 < len(ts):
                        e_off = max(0.0, min(seg_dur, ts[i + 1]))
                    else:
                        e_off = seg_dur
                    if e_off < s_off:
                        e_off = s_off
                    out.append(
                        {"word": str(tok), "start": seg_start_abs + s_off, "end": seg_start_abs + e_off}
                    )
                return out

            dur = max(0.0, seg_end_abs - seg_start_abs)
            step = dur / max(1, len(tokens))
            for i, tok in enumerate(tokens):
                s = seg_start_abs + i * step
                e = seg_start_abs + (i + 1) * step
                out.append({"word": str(tok), "start": s, "end": e})
            return out

        def _load_words_any_json(path: str) -> list:
            # 先尝试标准 JSON
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = None

            words = []

            def _extract_from_dict(d: dict):
                nonlocal words
                top_words = []
                seg_words = []

                # 1) 顶层 words（优先）
                ws = d.get("words")
                if isinstance(ws, list) and ws:
                    for w in ws:
                        nw = _normalize_word_obj(w) if isinstance(w, dict) else None
                        if nw:
                            top_words.append(nw)

                # 2) segments.words（仅在顶层 words 不足/缺失时使用；避免重复导致“时间轴被强行推到末尾”）
                segs = d.get("segments")
                if isinstance(segs, list) and segs:
                    for seg in segs:
                        if not isinstance(seg, dict):
                            continue
                        seg_ws = seg.get("words")
                        if isinstance(seg_ws, list) and seg_ws:
                            for w in seg_ws:
                                nw = _normalize_word_obj(w) if isinstance(w, dict) else None
                                if nw:
                                    seg_words.append(nw)

                        # tokens + timestamps（仅当两者都没有时才尝试）
                        if (not top_words) and (not seg_words):
                            try:
                                seg_start = float(seg.get("start", 0.0))
                                seg_end = float(seg.get("end", seg_start))
                            except Exception:
                                seg_start, seg_end = 0.0, 0.0
                            tokens = seg.get("tokens") or []
                            ts = seg.get("timestamps") or []
                            seg_words.extend(_build_words_from_tokens(tokens, ts, seg_start, seg_end))

                # 3) 选择一种来源（避免 top_words + seg_words 叠加导致重复 & 尾部“假延长”）
                chosen = []
                if top_words and seg_words:
                    try:
                        top_max = max(float(x.get("end", 0.0)) for x in top_words)
                        seg_max = max(float(x.get("end", 0.0)) for x in seg_words)
                    except Exception:
                        top_max, seg_max = 0.0, 0.0
                    if len(top_words) < int(len(seg_words) * 0.8) or (seg_max > top_max + 1.0):
                        chosen = seg_words
                    else:
                        chosen = top_words
                elif top_words:
                    chosen = top_words
                else:
                    chosen = seg_words

                # 4) 追加“有时间戳但缺 words”的极短 segments（常见于结尾的“嗯/啊”）
                tail_segs = []
                if isinstance(segs, list) and segs:
                    try:
                        chosen_max = max(float(x.get("end", 0.0)) for x in chosen) if chosen else 0.0
                    except Exception:
                        chosen_max = 0.0
                    for seg in segs:
                        if not isinstance(seg, dict):
                            continue
                        seg_ws = seg.get("words")
                        if isinstance(seg_ws, list) and seg_ws:
                            continue
                        txt = seg.get("text") or ""
                        txt = str(txt).strip()
                        if not txt:
                            continue
                        try:
                            ss = float(seg.get("start", 0.0))
                            ee = float(seg.get("end", ss))
                        except Exception:
                            continue
                        if ee <= chosen_max + 0.05:
                            continue
                        # 只追加“非常短”的内容，避免把大片未对齐文本硬塞进时间轴
                        if len(txt) <= 8 or (ee - ss) <= 2.0:
                            tail_segs.append({"word": txt, "start": ss, "end": ee})

                if tail_segs:
                    chosen = list(chosen) + tail_segs

                # 5) 排序 + 去重
                words = []
                if chosen:
                    chosen.sort(key=lambda x: (float(x.get("start", 0.0)), float(x.get("end", 0.0))))
                    seen = set()
                    for w in chosen:
                        key = (
                            str(w.get("word", "")),
                            round(float(w.get("start", 0.0)), 3),
                            round(float(w.get("end", 0.0)), 3),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        words.append(w)

            if isinstance(data, dict):
                _extract_from_dict(data)
                if words:
                    return words
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        _extract_from_dict(item)
                if words:
                    return words

            # fallback：解析 "start --> end : {payload}" 文本格式
            raw = ""
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read()
            if not raw.strip():
                return []

            records = []
            curr = None
            for line in raw.splitlines():
                mm = header_re.match(line)
                if mm:
                    if curr:
                        records.append(curr)
                    curr = (mm.group(1), mm.group(2), [mm.group(3)])
                else:
                    if curr:
                        curr[2].append(line)  # type: ignore[index]
            if curr:
                records.append(curr)

            for s_str, e_str, payload_lines in records:
                seg_start_abs = _time_to_seconds(s_str)
                seg_end_abs = _time_to_seconds(e_str)
                payload = "\n".join(payload_lines).strip()
                if not payload:
                    continue
                try:
                    d = json.loads(payload)
                except Exception:
                    lb = payload.find("{")
                    rb = payload.rfind("}")
                    if lb != -1 and rb != -1 and rb > lb:
                        try:
                            d = json.loads(payload[lb: rb + 1])
                        except Exception:
                            continue
                    else:
                        continue

                if isinstance(d, dict):
                    ws = d.get("words") or []
                    got = False
                    if isinstance(ws, list) and ws:
                        for w in ws:
                            if isinstance(w, dict):
                                nw = _normalize_word_obj(w)
                                if nw:
                                    words.append(nw)
                                    got = True
                    if got:
                        continue
                    tokens = d.get("tokens") or []
                    ts = d.get("timestamps") or d.get("timestamp") or []
                    words.extend(_build_words_from_tokens(tokens, ts, seg_start_abs, seg_end_abs))

            return words

        words = _load_words_any_json(json_path)
        if not words:
            raise ValueError("JSON 中未读取到有效 words（词级时间戳）。")

        cleaned = []
        for w in words:
            tok = str(w.get("word", ""))
            if not tok:
                continue
            if tok.strip() == "":
                continue
            item = {"word": tok, "start": float(w.get("start", 0.0)), "end": float(w.get("end", 0.0))}
            display_word = w.get("display_word")
            if isinstance(display_word, str) and display_word.strip():
                item["display_word"] = display_word
            cleaned.append(item)
        words = cleaned
        if not words:
            raise ValueError("words 为空（仅包含空白 token）。")

        MIN_WORD_DUR = 0.10
        MAX_SINGLE_CHAR_DUR = 1.50
        MAX_WORD_DUR = 4.00

        for i in range(len(words)):
            s = float(words[i]["start"])
            e = float(words[i]["end"])
            if i > 0:
                prev_end = float(words[i - 1]["end"])
                if s < prev_end:
                    s = prev_end
                    if e < s:
                        e = s
            if e <= s:
                if i + 1 < len(words):
                    ns = float(words[i + 1]["start"])
                    if ns > s:
                        e = min(ns, s + MAX_WORD_DUR)
                    else:
                        e = s + MIN_WORD_DUR
                else:
                    e = s + MIN_WORD_DUR

            if i + 1 < len(words):
                ns = float(words[i + 1]["start"])
                if ns > s and e > ns:
                    e = ns

            if (e - s) > MAX_WORD_DUR:
                e = s + MAX_WORD_DUR
            tok = str(words[i]["word"])
            if len(tok) == 1 and (e - s) > MAX_SINGLE_CHAR_DUR:
                e = s + MAX_SINGLE_CHAR_DUR
            if (e - s) < MIN_WORD_DUR:
                e = s + MIN_WORD_DUR
            words[i]["start"] = s
            words[i]["end"] = e

        filtered = []
        for w in words:
            tok = str(w["word"])
            dur = float(w["end"]) - float(w["start"])
            if self.ONECLICK_DROP_MOAN_ENABLED and self._oneclick_is_noise_token(tok, dur):
                continue
            filtered.append(w)
        words = filtered
        if not words:
            raise ValueError("去呻吟/噪音后 words 为空，请检查过滤规则或白名单。")

        n = len(words)
        target_sec = float(getattr(self, "ALIGN_TARGET_SECONDS", 4.0))
        soft_max = float(getattr(self, "ALIGN_SOFT_MAX_SECONDS", 7.0))
        hard_max = float(getattr(self, "ALIGN_MAX_DURATION_SECONDS", 10.0))
        pause_hard = float(getattr(self, "ONECLICK_HARD_PAUSE_SECONDS", 2.0))
        min_dur = float(getattr(self, "ALIGN_MIN_DURATION_SECONDS", 0.60))
        max_chars = int(getattr(self, "ALIGN_MAX_CHARS_PER_BLOCK", 46))

        strong_punct = set("。.!！？?…")
        weak_punct = set("、，,;；")

        def gap_after(k: int) -> float:
            if k + 1 >= n:
                return 0.0
            return max(0.0, float(words[k + 1]["start"]) - float(words[k]["end"]))

        def display_token(k: int) -> str:
            value = words[k].get("display_word")
            return str(value) if value not in (None, "") else str(words[k]["word"])

        def has_strong_boundary(k: int) -> bool:
            return any(ch in display_token(k) for ch in strong_punct)

        def has_weak_boundary(k: int) -> bool:
            return any(ch in display_token(k) for ch in weak_punct)

        def boundary_score(k: int) -> int:
            if has_strong_boundary(k):
                return 3
            if gap_after(k) >= 0.85:
                return 2
            if has_weak_boundary(k):
                return 1
            return 0

        def span_text(a: int, b: int) -> str:
            return "".join(display_token(i) for i in range(a, b + 1))

        spans = []
        i = 0
        while i < n:
            seg_start = float(words[i]["start"])
            j = i
            best = None
            while j < n:
                if j > i:
                    gap = float(words[j]["start"]) - float(words[j - 1]["end"])
                    if gap >= pause_hard:
                        break
                seg_end = float(words[j]["end"])
                dur = seg_end - seg_start
                if dur > hard_max:
                    break
                txt_len = len(span_text(i, j))
                score = boundary_score(j)

                if score >= 3 and dur >= min_dur:
                    best = (j, score, dur, txt_len)
                    break

                if dur >= target_sec:
                    cand = (j, score, dur, txt_len)
                    if best is None:
                        best = cand
                    else:
                        _, bs, bd, bl = best
                        if score > bs:
                            best = cand
                        elif score == bs:
                            if abs(dur - target_sec) < abs(bd - target_sec):
                                best = cand
                            elif abs(dur - target_sec) == abs(bd - target_sec) and txt_len < bl:
                                best = cand

                    if dur >= soft_max and best is not None and best[1] >= 1:
                        break
                    if txt_len >= max_chars and best is not None:
                        break

                j += 1

            if best is not None:
                end_idx = int(best[0])
            else:
                end_idx = max(i, min(j - 1, n - 1))

            if end_idx < n - 1:
                while end_idx < n - 1 and (float(words[end_idx]["end"]) - seg_start) < min_dur:
                    gap = float(words[end_idx + 1]["start"]) - float(words[end_idx]["end"])
                    if gap >= pause_hard:
                        break
                    end_idx += 1

            spans.append((i, end_idx))
            i = end_idx + 1

        def span_start_ms(a: int) -> int:
            return int(float(words[a]["start"]) * 1000)

        def span_end_ms(b: int) -> int:
            return int(float(words[b]["end"]) * 1000)

        def is_single_kana(t: str) -> bool:
            return bool(re.fullmatch(r"[\u3040-\u309f\u30a0-\u30ff]", t))

        def is_punct_only(t: str) -> bool:
            return self._oneclick_is_punct_only(t)

        TINY_ATTACH_GAP_MS = 400
        PUNCT_ATTACH_GAP_MS = 800

        merged = []
        k = 0
        while k < len(spans):
            a, b = spans[k]
            t = span_text(a, b).strip()

            if not t:
                k += 1
                continue

            if is_punct_only(t):
                if merged:
                    pa, pb = merged[-1]
                    gap_prev = max(0, span_start_ms(a) - span_end_ms(pb))
                    if gap_prev <= PUNCT_ATTACH_GAP_MS:
                        merged[-1] = (pa, b)
                        k += 1
                        continue
                if k + 1 < len(spans):
                    na, nb = spans[k + 1]
                    gap_next = max(0, span_start_ms(na) - span_end_ms(b))
                    if gap_next <= PUNCT_ATTACH_GAP_MS:
                        spans[k + 1] = (a, nb)
                        k += 1
                        continue
                k += 1
                continue

            if len(t) == 1 and is_single_kana(t) and t not in self.KANA_WHITELIST:
                if merged:
                    pa, pb = merged[-1]
                    gap_prev = max(0, span_start_ms(a) - span_end_ms(pb))
                    if gap_prev <= TINY_ATTACH_GAP_MS:
                        merged[-1] = (pa, b)
                        k += 1
                        continue
                if k + 1 < len(spans):
                    na, nb = spans[k + 1]
                    gap_next = max(0, span_start_ms(na) - span_end_ms(b))
                    if gap_next <= TINY_ATTACH_GAP_MS:
                        spans[k + 1] = (a, nb)
                        k += 1
                        continue
                merged.append((a, b))
                k += 1
                continue

            if self.ONECLICK_MOAN_ONLY_REGEX.match(t) and t not in self.KANA_WHITELIST:
                if merged:
                    pa, pb = merged[-1]
                    gap_prev = max(0, span_start_ms(a) - span_end_ms(pb))
                    if gap_prev <= int(getattr(self, "ONECLICK_TINY_MERGE_GAP_MS", 250)):
                        merged[-1] = (pa, b)
                k += 1
                continue

            merged.append((a, b))
            k += 1

        spans = merged
        # 再按字符数拆分，确保每条字幕在“两行限制”下不会出现超长行
        # 规则：最多 MAX_LINE_WIDTH * MAX_LINE_COUNT 个字符；超过则按词边界再切小
        max_chars_per_sub = int(getattr(self, "MAX_LINE_WIDTH", 30)) * int(
            getattr(self, "MAX_LINE_COUNT", 2)
        )
        if max_chars_per_sub > 0:
            new_spans = []
            for (a, b) in spans:
                cur_a = a
                while cur_a <= b:
                    cur_len = 0
                    last_punct = None
                    cut = None
                    j = cur_a
                    while j <= b:
                        tok = display_token(j)
                        cur_len += len(tok)
                        if any(ch in tok for ch in strong_punct) or any(ch in tok for ch in weak_punct):
                            last_punct = j
                        if cur_len >= max_chars_per_sub:
                            if last_punct is not None and last_punct >= cur_a:
                                cut = last_punct
                            else:
                                cut = j
                            break
                        j += 1
                    if cut is None:
                        new_spans.append((cur_a, b))
                        break
                    new_spans.append((cur_a, cut))
                    cur_a = cut + 1
            spans = new_spans
        if not spans:
            raise ValueError("拆短后 spans 为空（过滤规则可能过强）。")

        out_blocks = []
        spans_meta = []

        for (a, b) in spans:
            if a is None or b is None or a > b:
                continue
            raw_text = span_text(a, b).strip()
            if not raw_text:
                continue

            start_ms = span_start_ms(a)
            end_ms = span_end_ms(b)
            if end_ms <= start_ms:
                end_ms = start_ms + 100

            final_text = self._wrap_text(raw_text)
            text_for_match = final_text.strip()
            dur_ms = max(0, int(end_ms) - int(start_ms))
            # 删除“单行精确噪声词”“重复噪声词”，以及长时长下由噪声词组合且仅含标点/空白分隔的字幕。
            if self._is_single_line_ack_repeat_noise(text_for_match, duration_ms=dur_ms):
                continue

            out_blocks.append({"start_ms": start_ms, "end_ms": end_ms, "text": final_text})
            spans_meta.append(
                {
                    "a": int(a),
                    "b": int(b),
                    "start_ms": int(start_ms),
                    "end_ms": int(end_ms),
                    "text": raw_text,
                }
            )

        if not out_blocks:
            raise ValueError("未生成任何字幕条目，请检查输入或过滤规则。")

        MIN_DISPLAY_MS = int(getattr(self, "ONECLICK_MIN_DISPLAY_MS", 1300))
        MAX_DISPLAY_MS = int(getattr(self, "ONECLICK_MAX_DISPLAY_MS", int(hard_max * 1000)))
        NO_OVERLAP_GAP_MS = int(getattr(self, "ONECLICK_NO_OVERLAP_GAP_MS", 50))

        def _oneclick_max_display_ms_by_chars(text: str) -> int:
            t = str(text or "")
            chars = len(re.sub(r"\s+", "", t))
            if chars <= 0:
                return MIN_DISPLAY_MS
            cps = float(getattr(self, "ONECLICK_CHAR_CPS", 6.0))
            base_sec = float(getattr(self, "ONECLICK_CHAR_BASE_SEC", 0.6))
            sec = (chars / cps + base_sec) if cps > 0 else base_sec
            short_chars = int(getattr(self, "ONECLICK_SHORT_CHAR_CAP_CHARS", 6))
            short_cap_sec = float(getattr(self, "ONECLICK_SHORT_CHAR_CAP_SEC", 2.0))
            if chars <= short_chars:
                sec = min(sec, short_cap_sec)
            cap_sec = float(getattr(self, "ONECLICK_CHAR_CAP_MAX_SEC", 8.0))
            sec = min(sec, cap_sec, float(hard_max))
            sec = max(sec, MIN_DISPLAY_MS / 1000.0)
            return int(sec * 1000)

        for idx in range(len(out_blocks)):
            block = out_blocks[idx]
            s = int(block.get("start_ms", 0))
            e = int(block.get("end_ms", s))
            if e <= s:
                e = s + 100
            if MAX_DISPLAY_MS > 0 and (e - s) > MAX_DISPLAY_MS:
                e = s + MAX_DISPLAY_MS
            desired = max(e, s + MIN_DISPLAY_MS)

            char_cap_ms = _oneclick_max_display_ms_by_chars(block.get("text", ""))
            if char_cap_ms > 0:
                desired = min(desired, s + char_cap_ms)
            if idx + 1 < len(out_blocks):
                ns = int(out_blocks[idx + 1].get("start_ms", desired))
                limit = ns - NO_OVERLAP_GAP_MS
                if limit >= s + MIN_DISPLAY_MS and desired > limit:
                    desired = limit
            block["end_ms"] = int(desired)
            if idx < len(spans_meta):
                spans_meta[idx]["end_ms"] = int(desired)

        output_srt = f"{base}_split.srt"
        if os.path.exists(output_srt) and not os.access(output_srt, os.W_OK):
            output_srt = f"{base}_split_out.srt"
        output_content = [
            f"{i}\n{self.ms_to_time(b['start_ms'])} --> {self.ms_to_time(b['end_ms'])}\n{b['text']}"
            for i, b in enumerate(out_blocks, 1)
        ]
        with open(output_srt, "w", encoding="utf-8") as f:
            f.write("\n\n".join(output_content) + "\n\n")

        output_spans = f"{base}_split_spans.json"
        if os.path.exists(output_spans) and not os.access(output_spans, os.W_OK):
            output_spans = f"{base}_split_spans_out.json"
        meta = {
            "source_json": os.path.basename(json_path),
            "word_count_after_filter": len(words),
            "span_count": len(spans_meta),
            "spans": spans_meta,
        }
        with open(output_spans, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        return f"完成 (优化字幕): {os.path.basename(output_srt)}"

    def _repair_blocks_density(self, blocks):
        """修复字幕块的异常时长：字很少但占时很久时，提前结束该块（只缩短 end）。"""
        if not blocks:
            return blocks

        # 先按时间排序，方便拿 next_start
        blocks.sort(key=lambda b: (b.start_ms, b.end_ms))

        long_thr = float(getattr(self, "REPAIR_LONG_DURATION_SEC", 6.0))
        cps_min = float(getattr(self, "REPAIR_CPS_MIN", 1.0))
        target_cps = float(getattr(self, "REPAIR_TARGET_CPS", 5.0))
        min_sec = float(getattr(self, "REPAIR_COMPRESS_MIN_SEC", 0.8))
        max_sec = float(getattr(self, "REPAIR_COMPRESS_MAX_SEC", 3.0))
        short_chars = int(getattr(self, "REPAIR_SHORT_TEXT_CHARS", 3))
        short_max_sec = float(getattr(self, "REPAIR_SHORT_TEXT_MAX_SEC", 2.5))

        def count_chars(t: str) -> int:
            # 去掉空白/换行后计数（标点也算在内，足够保守）
            return len(re.sub(r"\s+", "", str(t)))

        for i, b in enumerate(blocks):
            dur_ms = max(0, int(b.end_ms) - int(b.start_ms))
            dur = dur_ms / 1000.0 if dur_ms > 0 else 0.0
            if dur <= 0:
                continue

            txt = b.text or ""
            chars = count_chars(txt)

            # 取下一条 start，防止 end 超过下一条导致重叠
            next_start_ms = None
            if i + 1 < len(blocks):
                next_start_ms = int(blocks[i + 1].start_ms)

            # 是否触发修复
            cps = (chars / dur) if dur > 0 else 999.0
            trigger = False
            if dur >= long_thr and cps < cps_min:
                trigger = True
            if chars <= short_chars and dur > short_max_sec:
                trigger = True

            if not trigger:
                continue

            # 估算一个更合理的显示时长（只用于缩短 end）
            desired = chars / target_cps if target_cps > 0 else dur
            desired = max(min_sec, min(max_sec, desired))

            new_end_ms = int(b.start_ms + desired * 1000)

            # 不要超过下一条 start - 50ms
            if next_start_ms is not None:
                new_end_ms = min(new_end_ms, max(int(b.start_ms), next_start_ms - 50))

            # 只允许缩短，不允许拉长
            if new_end_ms < b.end_ms:
                b.end_ms = new_end_ms

        return blocks



    def _extend_short_blocks_min_display(self, blocks):
        """根据字数动态延长过短字幕的显示时长（只延长end，不改变start）。
        规则：
        - 根据字数计算最小显示时长：5字以下1.5秒，15字以上5秒，中间线性插值
        - 只延长 end（往后多显示一会儿），不改变 start（避免字幕提前出现）
        - 若后方空间不足（碰到下一条字幕），就停在下一条开始前50ms
        """
        if not blocks:
            return blocks

        pad = int(CFG_DISPLAY_PAD_MS)

        def count_display_chars(text):
            return len(re.sub(r'\s+', '', str(text)))

        def calc_min_duration_ms(char_count):
            if char_count <= 5:
                return 1500
            elif char_count >= 15:
                return 5000
            else:
                return 1500 + (char_count - 5) * 350

        blocks.sort(key=lambda b: (b.start_ms, b.end_ms))

        for idx, b in enumerate(blocks):
            dur = b.end_ms - b.start_ms
            min_ms = calc_min_duration_ms(count_display_chars(b.text))

            if dur >= min_ms:
                continue

            # 计算允许延长的最大 end
            # 允许与下一条字幕重叠，但最多重叠到下一条字幕结束前500ms
            if idx + 1 < len(blocks):
                next_block = blocks[idx + 1]
                # 优先：下一条字幕开始前50ms（无重叠）
                # 次选：允许重叠，但不超过下一条字幕的中点或其结束前500ms
                max_end_no_overlap = next_block.start_ms - pad
                max_end_with_overlap = min(
                    next_block.start_ms + (next_block.end_ms - next_block.start_ms) // 2,
                    next_block.end_ms - 500
                )
                max_end_with_overlap = max(max_end_with_overlap, next_block.start_ms)

                # 如果无重叠空间不够，允许适度重叠
                if b.start_ms + min_ms <= max_end_no_overlap:
                    max_end = max_end_no_overlap
                else:
                    max_end = max_end_with_overlap
            else:
                max_end = b.start_ms + min_ms

            # 延长 end，但不超过 max_end
            new_end = min(b.start_ms + min_ms, max_end)
            new_end = max(new_end, b.start_ms + 50)  # 至少保持 50ms

            if new_end > b.end_ms:
                b.end_ms = new_end

        return blocks



    def _build_word_search_index(self, words):
        """
        把 words 拼成一个长字符串，方便用 find 找子串；
        同时建立 char->word_index 的映射，便于把“字符位置”还原回 words 的 i/j。
        另外返回每个 word 在 big_s 中的 [first_char, last_char]（若该 word 归一化后为空则为 -1）。
        """
        pieces = []
        char_to_word = []
        word_first_char = [-1] * len(words)
        word_last_char = [-1] * len(words)

        cur = 0
        for wi, w in enumerate(words):
            wn = self._normalize_for_match(w.get("word", ""))
            if not wn:
                continue
            word_first_char[wi] = cur
            pieces.append(wn)
            char_to_word.extend([wi] * len(wn))
            cur += len(wn)
            word_last_char[wi] = cur - 1

        return "".join(pieces), char_to_word, word_first_char, word_last_char

    def _align_srt_blocks_to_words(self, srt_blocks, words):
        """
        逐条把 SRT 的文本对齐到 words 序列，得到每条字幕对应的 words[i..j]。

        v10.6.6 改进：
        - 使用 pre.srt 的时间戳做“时间窗口约束”，只在该字幕时间前后各 N 秒范围内搜索，
          显著降低“重复短句/口头禅”错配到更后面位置的概率。
        - 时间窗口内先精确 find，失败再做小范围模糊匹配。
        - 若窗口内出现多处精确匹配，选择“更接近该字幕 start 时间”的那一处以降低重复句错配。
        """
        big_s, char_to_word, word_first_char, word_last_char = self._build_word_search_index(words)
        if not big_s:
            raise ValueError("JSON 里未找到有效 words，无法对齐。")

        # 预取时间轴，便于二分定位候选 words 范围
        starts = [float(w.get("start", 0.0)) for w in words]
        ends = [float(w.get("end", 0.0)) for w in words]
        win = float(self.ALIGN_TIME_SEARCH_WINDOW_SEC)
        fallback_time_only = bool(getattr(self, 'ALIGN_FALLBACK_TO_TIME_ONLY', False))

        def _time_only_span(_blk):
            """纯时间映射：用字幕时间范围在 words 里找覆盖段。"""
            st = max(0.0, _blk.start_ms / 1000.0)
            ed = max(st, _blk.end_ms / 1000.0)
            ii = bisect.bisect_left(ends, st)
            jj = bisect.bisect_right(starts, ed) - 1
            ii = max(0, min(ii, n_words - 1))
            jj = max(0, min(jj, n_words - 1))
            ii = max(ii, cursor_word)
            if jj < ii:
                return (None, None)
            return (ii, jj)

        # 光标：避免在同一窗口内回头匹配到前一句；允许在异常情况下“回滚”到时间窗口起点以自我纠偏
        cursor_char = 0
        cursor_word = 0

        results = []
        n_words = len(words)

        for blk in srt_blocks:
            q = self._normalize_for_match(blk.text)
            if not q:
                results.append((None, None, 0.0))
                continue

            # 以该字幕的时间为中心做候选范围： [start - win, end + win]
            low = max(0.0, blk.start_ms / 1000.0 - win)
            high = blk.end_ms / 1000.0 + win

            # 候选 word 索引范围（用 end>=low 与 start<=high 的交集）
            a = bisect.bisect_left(ends, low)
            b = bisect.bisect_right(starts, high) - 1
            a = max(0, min(a, n_words - 1))
            b = max(0, min(b, n_words - 1))
            a = max(a, cursor_word)

            if b < a:
                if fallback_time_only:
                    ii, jj = _time_only_span(blk)
                    if ii is not None:
                        cursor_word = max(cursor_word, jj + 1)
                        results.append((ii, jj, 0.0))
                        continue
                results.append((None, None, 0.0))
                continue

            # 映射到 big_s 的字符窗口
            cs = -1
            ce = -1
            for k in range(a, b + 1):
                if word_first_char[k] != -1:
                    cs = word_first_char[k]
                    break
            for k in range(b, a - 1, -1):
                if word_last_char[k] != -1:
                    ce = word_last_char[k]
                    break

            if cs == -1 or ce == -1 or cs > ce:
                if fallback_time_only:
                    ii, jj = _time_only_span(blk)
                    if ii is not None:
                        cursor_word = max(cursor_word, jj + 1)
                        results.append((ii, jj, 0.0))
                        continue
                results.append((None, None, 0.0))
                continue

            # 若上一次匹配走得太远，允许回滚到该时间窗口起点，避免级联错位
            if cursor_char > ce:
                cursor_char = cs

            local = big_s[cs : ce + 1]
            local_from = max(0, cursor_char - cs)

            # 1) 先精确匹配
            # 在窗口内可能出现多处相同短句（如“はい/そう”），这里枚举所有精确匹配，选最接近该字幕 start 时间的候选
            target_t = blk.start_ms / 1000.0
            best = None  # (time_diff, pos, i, j)
            pos_local = local.find(q, local_from)
            _cnt = 0
            while pos_local != -1:
                _cnt += 1
                if _cnt > 2000:
                    break
                pos = cs + pos_local
                start_char = pos
                end_char = pos + len(q) - 1
                if end_char <= ce and start_char < len(char_to_word) and end_char < len(char_to_word):
                    i = char_to_word[start_char]
                    j = char_to_word[end_char]
                    if i is not None and j is not None and i >= a and j <= b and i >= cursor_word:
                        td = abs(starts[i] - target_t)
                        if best is None or td < best[0]:
                            best = (td, pos, i, j)
                pos_local = local.find(q, pos_local + 1)

            if best is not None:
                _, pos, i, j = best
                cursor_char = pos + len(q)
                cursor_word = max(cursor_word, j + 1)
                results.append((i, j, 1.0))
                continue
            # 2) 小范围兜底：时间窗口内模糊匹配（比原来的 8000 字符全局窗口更收敛）
            qlen = len(q)
            if len(local) < qlen:
                if fallback_time_only:
                    ii, jj = _time_only_span(blk)
                    if ii is not None:
                        cursor_word = max(cursor_word, jj + 1)
                        results.append((ii, jj, 0.0))
                        continue
                results.append((None, None, 0.0))
                continue

            best_score = 0.0
            best_off = None
            best_td = None
            # 步长：窗口通常不大，尽量细；长句适当加步长省时间
            if qlen <= 80:
                step = 1
            elif qlen <= 160:
                step = 2
            else:
                step = 5

            # 仅从 local_from 起往后搜，避免回头
            for off in range(local_from, len(local) - qlen + 1, step):
                cand = local[off : off + qlen]
                # 快速剪枝：首尾字符不匹配时跳过（大幅减少 SequenceMatcher 调用）
                if cand and (cand[0] != q[0] or cand[-1] != q[-1]):
                    continue
                score = difflib.SequenceMatcher(None, q, cand).ratio()

                # 以分数为主，若分数相同则选更接近字幕 start 时间的候选
                td = None
                if score >= best_score - 1e-9:
                    pos = cs + off
                    start_char = pos
                    end_char = pos + qlen - 1
                    if end_char <= ce and start_char < len(char_to_word) and end_char < len(char_to_word):
                        i0 = char_to_word[start_char]
                        if i0 is not None:
                            td = abs(starts[i0] - (blk.start_ms / 1000.0))

                if score > best_score + 1e-9:
                    best_score = score
                    best_off = off
                    best_td = td
                elif abs(score - best_score) <= 1e-9 and td is not None:
                    if best_td is None or td < best_td:
                        best_off = off
                        best_td = td
            if best_off is not None and best_score >= 0.90:
                pos = cs + best_off
                start_char = pos
                end_char = pos + qlen - 1
                if end_char <= ce and start_char < len(char_to_word) and end_char < len(char_to_word):
                    i = char_to_word[start_char]
                    j = char_to_word[end_char]
                    cursor_char = pos + qlen
                    cursor_word = max(cursor_word, j + 1)
                    results.append((i, j, round(best_score, 3)))
                    continue

            if fallback_time_only:
                ii, jj = _time_only_span(blk)
                if ii is not None:
                    cursor_word = max(cursor_word, jj + 1)
                    results.append((ii, jj, 0.0))
                    continue
            results.append((None, None, 0.0))

        return results

    def _split_word_span(self, words, i, j):
        """
        把一个 words[i..j] 的大段拆成更短的多个 span（[(a,b), ...]）。
        目标：平均 3-5 秒、单条不超过 10 秒；并且能切开“跨场景/跨说话”的离谱合并（200+ 秒）。
        关键：时间只来自 words 的 start/end，不做“按字数比例分摊时间”。
        """
        spans = []
        if i is None or j is None or i > j:
            return spans

        target_sec = float(self.ALIGN_TARGET_SECONDS)
        soft_max = float(self.ALIGN_SOFT_MAX_SECONDS)
        hard_max = float(self.ALIGN_MAX_DURATION_SECONDS)
        min_dur = float(self.ALIGN_MIN_DURATION_SECONDS)
        max_chars = int(self.ALIGN_MAX_CHARS_PER_BLOCK)
        min_chars = int(self.ALIGN_MIN_CHARS_PER_BLOCK)
        pause_soft = float(self.ALIGN_PAUSE_SOFT_SPLIT_SEC)
        pause_hard = float(self.ALIGN_PAUSE_HARD_SPLIT_SEC)
        pause_super = float(self.ALIGN_PAUSE_SUPER_SPLIT_SEC)

        def wtxt(k):
            w = words[k]
            if isinstance(w, dict):
                return str(w.get('word') or w.get('text') or w.get('token') or '')
            return str(w)

        # 预计算字符前缀和，减少重复统计
        prefix = [0] * (j + 2)
        for idx in range(i, j + 1):
            prefix[idx + 1] = prefix[idx] + len(wtxt(idx))

        def char_cnt(a, b):
            return prefix[b + 1] - prefix[a]

        def seg_dur(a, b):
            return float(words[b]['end']) - float(words[a]['start'])

        def gap_after(k):
            if k + 1 > j:
                return 0.0
            return float(words[k + 1]['start']) - float(words[k]['end'])

        def boundary_rank(k):
            """返回切点优先级：强标点(3) > 软停顿(2) > 弱标点(1) > 其它(0)"""
            t = wtxt(k)
            # 强标点：最自然的一句话结束
            if any(ch in t for ch in ['。', '！', '？', '!', '?', '…', '……']):
                return 3
            # 软停顿：听感上也像一句结束（更谨慎使用）
            if gap_after(k) >= pause_soft:
                return 2
            # 弱标点：逗号/顿号（最不自然，实在需要时用）
            if any(ch in t for ch in ['、', '，', ',', '；', ';']):
                return 1
            return 0

        # Step 0：先按“硬停顿”强制切开（专门解决 200+ 秒离谱合并）
        hard_thr_hard = pause_hard
        hard_thr_super = pause_super
        chunks = []
        s = i
        for k in range(i, j):
            if gap_after(k) >= hard_thr_super or gap_after(k) >= hard_thr_hard:
                chunks.append((s, k))
                s = k + 1
        chunks.append((s, j))

        # Step 1：在每个 chunk 内，按软上限/硬上限继续拆短
        for (cs, ce) in chunks:
            if cs > ce:
                continue
            cur = cs
            while cur <= ce:
                start_t = float(words[cur]['start'])

                # 先找到在 hard_max + max_chars 约束下，能吃到的最远 end
                best_end = cur
                for k in range(cur, ce + 1):
                    if seg_dur(cur, k) <= hard_max and char_cnt(cur, k) <= max_chars:
                        best_end = k
                    else:
                        break

                # 如果剩余全部能吃下，并且不算太长，就直接收尾
                if best_end == ce and seg_dur(cur, best_end) <= soft_max and char_cnt(cur, best_end) <= max_chars:
                    spans.append((cur, best_end))
                    break

                # 需要切：优先找自然断点（强标点/软停顿/弱标点）
                desired_t = start_t + target_sec
                soft_t = start_t + soft_max

                candidates = []  # (rank, abs(end_t-desired), k)  rank 越高越好，abs 越小越好
                for k in range(cur, best_end):
                    d = seg_dur(cur, k)
                    if d < min_dur:
                        continue
                    if char_cnt(cur, k) < min_chars:
                        continue
                    r = boundary_rank(k)
                    if r <= 0:
                        continue
                    end_t = float(words[k]['end'])
                    # 仅用于评分：更靠近目标更好，但不强制
                    candidates.append((r, abs(end_t - desired_t), k))

                cut = None

                # 1) 先尝试：目标附近（±1.0s）是否有自然断点
                if candidates:
                    near = [(r, dist, k) for (r, dist, k) in candidates if abs((float(words[k]['end']) - desired_t)) <= 1.0]
                    if near:
                        near.sort(key=lambda x: (-x[0], x[1], -x[2]))
                        cut = near[0][2]

                # 2) 再尝试：找 soft_max 之前“最靠后”的自然断点（避免切得太碎）
                if cut is None and candidates:
                    ok = [(r, k) for (r, dist, k) in candidates if float(words[k]['end']) <= soft_t]
                    if ok:
                        # rank 高优先，其次尽量靠后
                        ok.sort(key=lambda x: (-x[0], -x[1]))
                        cut = ok[0][1]

                # 3) 还找不到：就按时间硬切（尽量靠近 soft_max；如果连 soft_max 都不够，就用 best_end）
                if cut is None:
                    # 找到 end 时间 <= soft_t 的最靠后 word；否则用 best_end
                    cut = best_end
                    for k in range(best_end, cur - 1, -1):
                        if seg_dur(cur, k) >= min_dur and char_cnt(cur, k) >= min_chars and float(words[k]['end']) <= soft_t:
                            cut = k
                            break

                # 最终兜底：保证至少推进 1 个 word
                if cut < cur:
                    cut = cur

                spans.append((cur, cut))
                cur = cut + 1

        # Step 2：尾段太短就并回上一条（不超过 hard_max 才并）
        if len(spans) >= 2:
            a, b = spans[-1]
            if seg_dur(a, b) < min_dur or char_cnt(a, b) < min_chars:
                pa, pb = spans[-2]
                if seg_dur(pa, b) <= hard_max and char_cnt(pa, b) <= max_chars:
                    spans[-2] = (pa, b)
                    spans.pop()

        return spans

    def _wrap_text(self, text):
        """仅换行排版：不拆成新的字幕块。"""
        source_text = str(text).replace("\n", " ").strip()
        if not source_text:
            return ""
        lines = []
        temp_text = source_text
        while len(temp_text) > 0:
            if len(temp_text) <= self.MAX_LINE_WIDTH:
                lines.append(temp_text)
                break
            cut_pos = -1
            for i in range(self.MAX_LINE_WIDTH, 0, -1):
                if i < len(temp_text) and temp_text[i] in "。、，, 。！？!? ":
                    cut_pos = i + 1
                    break
            if cut_pos == -1:
                cut_pos = self.MAX_LINE_WIDTH
            lines.append(temp_text[:cut_pos].strip())
            temp_text = temp_text[cut_pos:].strip()
        # 尽量控制在 MAX_LINE_COUNT 行，但不丢字：多出来的内容塞进最后一行
        if len(lines) <= self.MAX_LINE_COUNT:
            return "\n".join(lines)
        head = lines[: self.MAX_LINE_COUNT - 1]
        tail = " ".join(lines[self.MAX_LINE_COUNT - 1 :]).strip()
        return "\n".join(head + [tail])

    def process_align_split_group(self, json_path: str, ja_srt_path: str):
        """
        对齐拆短：输入【JSON + 日文语义分割 SRT】 -> 输出【更短、更好读、时间更准的日文 SRT 中间产物】
        关键点：时间来自 words，不用“按字数比例分摊”。
        """
        words = self._load_whisper_words(json_path)
        # 修复极少数 time-stamp 异常（例如单个词拖十几秒）
        words = self._repair_word_durations(words)

        with open(ja_srt_path, "r", encoding="utf-8") as f:
            srt_blocks = self.parse_srt(f.read())

        if not srt_blocks:
            return "错误: 未能从日文 SRT 解析任何有效内容。"

        align = self._align_srt_blocks_to_words(srt_blocks, words)

        out_blocks = []
        for b, (i, j, score) in zip(srt_blocks, align):
            if i is None or j is None:
                # 找不到就跳过（也可以改成保留原块）
                continue
            spans = self._split_word_span(words, i, j)
            for (a, c) in spans:
                start_ms = int(float(words[a]["start"]) * 1000)
                end_ms = int(float(words[c]["end"]) * 1000)
                txt = "".join(str(words[x].get("display_word") or words[x]["word"]) for x in range(a, c + 1))
                txt = self._wrap_text(txt)
                out_blocks.append(self.SubtitleBlock(0, start_ms, end_ms, txt))
        # 修复“字很少但占时很久”的异常块（只缩短 end，不会影响对齐结果）
        out_blocks = self._repair_blocks_density(out_blocks)
        # 强制消除相邻字幕时间重叠：如果上一条的 end 晚于下一条的 start，则把上一条 end 截到下一条 start 前一点点
        # 说明：重叠通常来自“边界词被分到前后两条”或对齐边界的小偏差；这里只做显示层面的收敛（只缩短 end，不移动 start）
        out_blocks.sort(key=lambda b: (b.start_ms, b.end_ms))
        for k in range(len(out_blocks) - 1):
            if out_blocks[k].end_ms > out_blocks[k + 1].start_ms:
                # 预留 50ms 间隙，避免播放器闪烁；同时保证 end 不小于 start + 50ms
                new_end = out_blocks[k + 1].start_ms - 50
                min_end = out_blocks[k].start_ms + 50
                out_blocks[k].end_ms = max(min_end, new_end)


        # 【可读性】延长过短字幕的显示时长（不改变对齐，只在相邻空档里拉长）
        out_blocks = self._extend_short_blocks_min_display(out_blocks)

        # 再跑一次去重叠（保险）：延长/前移 start 后仍保证完全不重叠
        out_blocks.sort(key=lambda b: (b.start_ms, b.end_ms))
        for k in range(len(out_blocks) - 1):
            if out_blocks[k].end_ms > out_blocks[k + 1].start_ms:
                new_end = out_blocks[k + 1].start_ms - 50
                min_end = out_blocks[k].start_ms + 50
                out_blocks[k].end_ms = max(min_end, new_end)




        # 先过滤“单行精确噪声词”“重复噪声词”，以及长时长下由噪声词组合且仅含标点/空白分隔的条目（与一键拆短规则保持一致）
        out_blocks = [
            b for b in out_blocks
            if not (
                self._is_single_line_ack_repeat_noise(
                    b.text,
                    duration_ms=max(0, int(b.end_ms) - int(b.start_ms)),
                )
            )
        ]

        # 过滤单行纯标点或单字（多行保留）
        punctuation = "。、，！？…・「」『』（）!?,."
        out_blocks = [
            b for b in out_blocks
            if "\n" in b.text or len(b.text.strip(punctuation)) >= 2
        ]

        # 重新编号并输出
        base, _ = os.path.splitext(ja_srt_path)
        output_filepath = f"{base}_fixed.srt"
        output_content = [
            f"{i}\n{self.ms_to_time(b.start_ms)} --> {self.ms_to_time(b.end_ms)}\n{b.text}"
            for i, b in enumerate(out_blocks, 1)
        ]
        with open(output_filepath, "w", encoding="utf-8") as f:
            f.write("\n\n".join(output_content) + "\n\n")
        return f"完成 (中间产物): {os.path.basename(output_filepath)}"

    def process_file(
        self, filepath, do_cleaning, do_formatting, punc_split, ellipses_mode, merge_short
    ):
        """旧模式（步骤1/步骤2/完整流程）：支持 SRT 与 JSON/TXT。
        - 输入 JSON/TXT：解析 ->（可选）清洗/格式化 -> 输出 *_cleaned_whisper.json
        - 输入 SRT：解析 ->（可选）清洗/格式化 -> 输出 *_processed.srt
        """
        base, ext = os.path.splitext(filepath)
        ext_lower = ext.lower()

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return f"错误: 无法读取文件 {os.path.basename(filepath)}。原因: {e}"

        # 1) 解析输入
        if ext_lower in [".json", ".txt"]:
            blocks = self.parse_json(content)
            is_json_input = True
        elif ext_lower == ".srt":
            blocks = self.parse_srt(content)
            is_json_input = False
        else:
            return f"错误: 不支持的文件格式 {ext}"

        if not blocks:
            return f"错误: 未能从 {os.path.basename(filepath)} 解析任何有效内容。"

        # 2) 预处理（清洗）
        if do_cleaning:
            blocks = self.clean_blocks(blocks)
            blocks = self.clean_overlapping_noise(blocks)
            blocks = self.merge_consecutive_duplicates(blocks)

        # 3) 格式化（断句/合并/换行）
        if do_formatting:
            blocks = self.format_blocks(blocks, punc_split, ellipses_mode, merge_short)

        blocks = [b for b in blocks if b.text.strip()]
        if not blocks:
            return f"错误: 处理后没有可输出的字幕内容。"

        # 3.5) 延长过短字幕的显示时长（不改变对齐，只在相邻空档里拉长）
        blocks = self._extend_short_blocks_min_display(blocks)

        # 4) 导出输出
        try:
            if is_json_input:
                output_filepath = f"{base}_cleaned_whisper.json"
                whisper_data = self.generate_whisper_json(blocks)
                with open(output_filepath, "w", encoding="utf-8") as f:
                    json.dump(whisper_data, f, ensure_ascii=False, indent=4)
                return f"完成 (JSON): {os.path.basename(output_filepath)}"
            else:
                output_content = [
                    f"{i}\n{self.ms_to_time(b.start_ms)} --> {self.ms_to_time(b.end_ms)}\n{b.text}"
                    for i, b in enumerate(blocks, 1)
                ]
                output_filepath = f"{base}_processed.srt"
                with open(output_filepath, "w", encoding="utf-8") as f:
                    f.write("\n\n".join(output_content) + "\n\n")
                return f"完成 (SRT): {os.path.basename(output_filepath)}"
        except Exception as e:
            return f"错误: 无法写入文件。原因: {e}"

    def process_post_translation_cleanup(self, srt_path: str) -> str:
        base, ext = os.path.splitext(srt_path)
        if ext.lower() != ".srt":
            raise ValueError("译后清理仅支持 SRT 输入（*.srt）。")

        try:
            with open(srt_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            raise ValueError(f"无法读取文件 {os.path.basename(srt_path)}。原因: {e}") from e

        blocks = self.parse_srt(content)
        if not blocks:
            raise ValueError(f"未能从 {os.path.basename(srt_path)} 解析任何有效内容。")

        blocks = [
            block for block in blocks
            if not self._is_single_line_post_translation_noise(block.text)
        ]
        if not blocks:
            raise ValueError("译后清理后没有可输出的字幕内容，请检查过滤规则。")

        output_filepath = f"{base}_post_cleaned.srt"
        output_content = [
            f"{i}\n{self.ms_to_time(block.start_ms)} --> {self.ms_to_time(block.end_ms)}\n{block.text}"
            for i, block in enumerate(blocks, 1)
        ]
        try:
            with open(output_filepath, "w", encoding="utf-8") as f:
                f.write("\n\n".join(output_content) + "\n\n")
        except Exception as e:
            raise ValueError(f"无法写入文件 {os.path.basename(output_filepath)}。原因: {e}") from e

        return f"完成 (译后清理): {os.path.basename(output_filepath)}"
