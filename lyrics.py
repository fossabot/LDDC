# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright (c) 2024 沉默の金
import logging
import re

from bs4 import BeautifulSoup

from api import get_qrc, qm_get_lyric
from decryptor import qrc_decrypt


class LyricType:
    LRC = 0
    QRC = 1


class LyricProcessingError:
    REQUEST = 0
    DECRYPT = 1
    NOT_FOUND = 2


def judge_lyric_type(text: str) -> LyricType:
    if "<?xml " in text[:10] or "<QrcInfos>" in text[:10]:
        return LyricType.QRC
    return LyricType.LRC


def has_content(line: str) -> bool:
    """检查是否有实际内容"""
    content = re.sub(r"\[\d+:\d+\.\d+\]|\[\d+,\d+\]", "", line).strip()
    if content in ("", "//"):
        return False
    return True


def get_clear_lyric(lyric: str) -> str:
    # 为保证find_closest_match的准确性不可直接用于原歌词
    result = []
    for line in lyric.splitlines():
        if has_content(line):
            result.append(line)
    return "\n".join(result)


def ms2formattime(ms: int) -> str:
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{int(m):02d}:{int(s):02d}.{int(ms):03d}"


def time2ms(m: int, s: int, ms: int) -> int:
    """时间转毫秒"""
    return int((m * 60 + s) * 1000 + ms)


def qrc2lrc(qrc: str) -> str:
    """将明文qrc转换为lrc"""
    qrc_lines = qrc.split('\n')
    lrc_lines = []
    wrods_split_pattern = re.compile(r'(?:\[\d+,\d+\])?((?:(?!\(\d+,\d+\)).)+)\((\d+),(\d+)\)')  # 逐字匹配
    lines_split_pattern = re.compile(r'\[(\d+),(\d+)\](.*)$')  # 逐行匹配

    for line in qrc_lines:
        line = line.strip()  # noqa: PLW2901
        lines_split_content = re.findall(lines_split_pattern, line)
        if lines_split_content:  # 判断是否为歌词行
            line_start_time, line_duration, line_content = lines_split_content[0]
            wrods_split_content = re.findall(wrods_split_pattern, line)
            if wrods_split_content:  # 判断是否为逐字歌词
                lrc_line, last_time = "", None
                for text, starttime, duration in wrods_split_content:
                    if text != "\r":
                        if int(starttime) != last_time:  # 判断开始时间是否等于上一个结束时间
                            lrc_line += f"[{ms2formattime(int(starttime))}]{text}[{ms2formattime(int(starttime) + int(duration))}]"
                        else:
                            lrc_line += f"{text}[{ms2formattime(int(starttime) + int(duration))}]"
                        last_time = int(starttime) + int(duration)  # 结束时间
            else:  # 如果不是逐字歌词
                lrc_line = f"[{ms2formattime(int(line_start_time))}]{line_content}[{ms2formattime(int(line_start_time) + int(line_duration))}]"
            lrc_lines.append(lrc_line)
        else:
            lrc_lines.append(line)
            continue
    return '\n'.join(lrc_lines)


def find_closest_match(list1: list, list2: list) -> dict:
    list1 = list1[:]
    list2 = list2[:]
    # 存储合并结果的列表
    merged_dict = {}

    # 遍历第一个列表中的每个时间戳和歌词
    i = 0
    while len(list1) > i:
        timestamp1, lyrics1 = list1[i]
        # 找到在第二个列表中最接近的匹配项
        closest_timestamp2, closest_lyrics2 = min(list2, key=lambda x: abs(x[0] - timestamp1))

        if (closest_timestamp2, closest_lyrics2) not in merged_dict:
            merged_dict.update({(closest_timestamp2, closest_lyrics2): (timestamp1, lyrics1)})
        elif abs(timestamp1 - closest_timestamp2) < abs(merged_dict[(closest_timestamp2, closest_lyrics2)][0] - closest_timestamp2):
            list1.append(merged_dict[(closest_timestamp2, closest_lyrics2)])
            merged_dict[(closest_timestamp2, closest_lyrics2)] = (timestamp1, lyrics1)
        else:
            available_items = [(item[0], item[1]) for item in list2 if item not in merged_dict]
            if available_items:
                closest_timestamp2, closest_lyrics2 = min(available_items, key=lambda x: abs(x[0] - timestamp1))
                merged_dict[(closest_timestamp2, closest_lyrics2)] = (timestamp1, lyrics1)
            else:
                logging.warning(f"{timestamp1, lyrics1}无法匹配")

        i += 1

    sorted_items = sorted(((value[0], value[1], key[0], key[1]) for key, value in merged_dict.items()), key=lambda x: x[0])

    return {(item[0], item[1]): (item[2], item[3]) for item in sorted_items}


class Lyrics(dict):
    def __init__(self, info: dict) -> None:
        self.source = info["source"]
        self.orig_type = None
        self.title = info['title']
        self.artist = info["artist"]
        self.album = info["album"]
        self.id = info["id"]
        self.mid = info["mid"]

    def download_and_decrypt(self) -> tuple[str | None, int | None]:
        """
        下载与解密歌词
        :return: 错误
        """
        if self.source not in ["qm"]:
            return "不支持的源"

        match self.source:
            case "qm":
                response = get_qrc(self.id)
                if isinstance(response, str):
                    return f"请求qrc歌词失败,错误:{response}", LyricProcessingError.REQUEST
                qrc_xml = re.sub(r"^<!--|-->$", "", response.text.strip())
                qrc_suop = BeautifulSoup(qrc_xml, 'xml')
                for key, value in [("orig", 'content'),
                                   ("ts", 'contentts'),
                                   ("roma", 'contentroma')]:
                    find_result = qrc_suop.find(value)
                    if find_result is not None and find_result['timetag'] != "0":
                        encrypted_lyric = find_result.get_text()

                        cannot_decrypt = ["789C014000BFFF", "789C014800B7FF"]
                        for c in cannot_decrypt:
                            if encrypted_lyric.startswith(c):
                                return f"没有获取到可解密的歌词(encrypted_lyric starts with {c})", LyricProcessingError.NOT_FOUND

                        lyric, error = qrc_decrypt(encrypted_lyric)

                        if lyric is not None:
                            type_ = judge_lyric_type(lyric)
                            if type_ == LyricType.QRC:
                                lyric = qrc2lrc(re.findall(r'<Lyric_1 LyricType="1" LyricContent="(.*?)"/>', lyric, re.DOTALL)[0])
                            self[key] = lyric
                            self.orig_type = "qrc"
                        elif error is not None:
                            return "解密歌词失败, 错误: " + error, LyricProcessingError.DECRYPT
                    elif (find_result['timetag'] == "0" and key == "orig"):
                        return "没有获取到可解密的歌词(timetag=0)", LyricProcessingError.NOT_FOUND

        if self["orig"] is None:
            return "没有获取到可解密的歌词(orig=None)", LyricProcessingError.NOT_FOUND
        return None, None

    def download_normal_lyrics(self) -> tuple[str | None, int | None]:
        result = qm_get_lyric(self.mid)
        if isinstance(result, str):
            return f"请求普通歌词时错误: {result}", LyricProcessingError.REQUEST
        orig, ts = result
        if orig is not None:
            self["orig"] = orig
            self.orig_type = "lrc"
        if ts is not None:
            self["ts"] = ts
            self.orig_type = "lrc"
        if self["orig"] is None and self["ts"] is None:
            return "没有获取到可用的歌词(orig=None and ts=None)", LyricProcessingError.NOT_FOUND
        return None, None

    def merge(self: dict[str: str], lyrics_order: list) -> str:
        """
        合并歌词
        :param lyrics_order:歌词顺序,同时决定需要合并的类型
        :return: 合并后的歌词
        """
        match len(lyrics_order):
            case 0:
                logging.warning("没有需要合并的歌词")
                return ""
            case 1:
                return get_clear_lyric(self[lyrics_order[0]])

        lyrics = [(key, lyric) for key, lyric in self.items() if key in lyrics_order]
        if len(lyrics) == 1:
            return get_clear_lyric(lyrics[0][1])

        if 'orig' not in lyrics:  # 确保只勾选译文与罗马音时正常合并时
            lyrics.append(('orig', self['orig']))

        time_text_split_pattern = re.compile(r'^\[(\d+):(\d+)\.(\d+)\](.*)$')
        tag_split_pattern = re.compile(r"^\[([A-Za-z]+):(.*)\]\r?$")
        end_time_pattern = re.compile(r"(\[\d+:\d+\.\d+\])$")

        lyric_tagkeys = []
        lyric_times = {}  # key 可能包含 'orig' 'ts' 'roma'
        mapping_tables = {}
        lyric_lines = []

        for key, lyric in lyrics:
            lyric_times[key] = []
            for line in lyric.splitlines():
                if line.startswith('['):
                    lyric_line = time_text_split_pattern.match(line)
                    if lyric_line:  # 歌词行
                        m, s, ms, text = lyric_line.groups()
                        time_ = time2ms(int(m), int(s), int(ms))
                        lyric_times[key].append((time_, text))
                        continue
                    tag = tag_split_pattern.match(line)
                    if tag:  # 标签行
                        if tag.groups()[0] not in lyric_tagkeys:
                            lyric_tagkeys.append(tag.groups()[0])
                            lyric_lines.append(line)
                        continue
                    logging.error(f"未知类型的行: {line}")

        if "ts" in lyric_times:
            mapping_tables["ts"] = find_closest_match(lyric_times["orig"], lyric_times["ts"])
        if "roma" in lyric_times:
            mapping_tables["roma"] = find_closest_match(lyric_times["orig"], lyric_times["roma"])

        def get_full_line(mapping_table: dict, orig_time: int, orig_line: str) -> str:
            line = mapping_table[(orig_time, orig_line)][1]  # 无第一个时间戳
            if not has_content(line):
                return ""
            if re.search(end_time_pattern, line):    # 检查是否有结束时间
                return f"[{ms2formattime(orig_time)}]{line}"
            end_time_matched = re.findall(end_time_pattern, orig_line)
            orig_end_formattime = end_time_matched[0] if end_time_matched else ""
            return f"[{ms2formattime(orig_time)}]{line}{orig_end_formattime}"

        for orig_time, orig_line in lyric_times["orig"]:  # orig_line无第一个时间戳,orig_time为第一个时间戳(ms类型)
            lines = ""
            full_orig_line = f"[{ms2formattime(orig_time)}]" + orig_line  # 此时line为完整的原文歌词行

            for type_ in lyrics_order:
                if type_ == "orig":
                    line = full_orig_line
                elif type_ in mapping_tables:
                    line = get_full_line(mapping_tables[type_], orig_time, orig_line)
                else:
                    continue

                if lines != "" and line != "":
                    lines += "\n" + line
                else:
                    lines += line

            lyric_lines.append(lines)

        return "\n".join(lyric_lines)
