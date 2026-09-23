"""
实体消歧模块 - 识别同义实体（简称/全称/别名）并归一到标准实体名

识别策略（纯规则，按优先级组合）：
1. 内置/扩展别名词典：如“北大”->“北京大学”、“中科院”->“中国科学院”
2. 机构后缀剥离：核心名相同视为同义，如“腾讯”vs“腾讯公司”
3. 前缀包含：短名是长名的前缀且剩余部分为机构后缀，如“阿里巴巴”vs“阿里巴巴集团”
4. 中文简称子序列规则：短名首字与长名首字相同，且短名是长名的子序列，
   如“北大”⊂“北京大学”、“中科院”⊂“中国科学院”

安全约束：只有同类型实体才会被判定为同义，避免“北京”(LOCATION)
与“北京大学”(ORG) 这类同名不同义的实体被误合并。
"""
import unicodedata
from typing import Dict, List, Optional, Tuple

from backend.utils.config import BUILTIN_ALIASES, ORG_SUFFIXES

# 允许使用“后缀剥离/前缀包含/子序列简称”规则的实体类型
# （简称现象主要出现在机构类实体上，人名等类型误合并风险高，不启用）
ABBREVIATION_TYPES = {'ORG'}


class EntityResolver:
    """实体消歧器"""

    def __init__(self, extra_aliases: Dict[str, str] = None):
        # 内置别名表 + 调用方扩展别名表（key 统一规范化）
        self._dictionary: Dict[str, str] = {}
        for alias, full in BUILTIN_ALIASES.items():
            self._dictionary[self.normalize(alias)] = full
        for alias, full in (extra_aliases or {}).items():
            self._dictionary[self.normalize(alias)] = full

    # ==================== 名称规范化 ====================

    @staticmethod
    def normalize(text: str) -> str:
        """规范化实体名：全角转半角、去空白和常见标点、拉丁字母转小写"""
        if not text:
            return ''
        text = unicodedata.normalize('NFKC', text)
        punctuation = "-_·•.,，。()（）[]【】\"'“”‘’"
        text = ''.join(ch for ch in text
                       if not ch.isspace() and ch not in punctuation)
        return text.lower()

    # ==================== 同义判定 ====================

    def lookup_dictionary(self, text: str) -> Optional[str]:
        """查别名词典，返回标准全称，未命中返回 None"""
        return self._dictionary.get(self.normalize(text))

    @staticmethod
    def strip_org_suffix(text: str) -> str:
        """循环剥离机构后缀，得到核心名，如“阿里巴巴有限公司”->“阿里巴巴”"""
        core = text
        changed = True
        while changed:
            changed = False
            for suffix in ORG_SUFFIXES:
                if core.endswith(suffix) and len(core) > len(suffix):
                    core = core[:-len(suffix)]
                    changed = True
                    break
        return core

    @staticmethod
    def _is_subsequence(short: str, long: str) -> bool:
        """判断 short 是否为 long 的子序列（字符按顺序出现）"""
        it = iter(long)
        return all(ch in it for ch in short)

    def are_synonyms(self, name_a: str, name_b: str,
                     type_a: str, type_b: str) -> bool:
        """判断两个实体名是否指向同一实体"""
        # 类型不同不合并（如“北京”LOCATION 与“北京大学”ORG）
        if type_a != type_b:
            return False
        if name_a == name_b:
            return True

        na, nb = self.normalize(name_a), self.normalize(name_b)
        if not na or not nb:
            return False
        if na == nb:
            return True

        # 规则1：别名词典（两个名称中任一方是另一方的登记别名，
        # 或两个别名登记到同一标准名）
        full_a = self.lookup_dictionary(name_a)
        full_b = self.lookup_dictionary(name_b)
        if full_a and self.normalize(full_a) == nb:
            return True
        if full_b and self.normalize(full_b) == na:
            return True
        if full_a and full_b and self.normalize(full_a) == self.normalize(full_b):
            return True

        # 以下缩写规则仅对机构类实体启用，降低误合并风险
        if type_a not in ABBREVIATION_TYPES:
            return False

        # 规则2：剥离机构后缀后核心名相同（“腾讯”vs“腾讯公司”）
        core_a = self.strip_org_suffix(name_a)
        core_b = self.strip_org_suffix(name_b)
        if len(core_a) >= 2 and core_a == core_b:
            return True

        short, long = (na, nb) if len(na) <= len(nb) else (nb, na)

        # 规则3：短名是长名的前缀，且剩余部分是机构后缀
        # （“阿里巴巴”vs“阿里巴巴集团”）
        if len(short) >= 2 and long.startswith(short):
            remainder = long[len(short):]
            if remainder in [self.normalize(s) for s in ORG_SUFFIXES]:
                return True

        # 规则4：中文简称子序列规则——短名首字与长名首字相同，
        # 且短名是长名的子序列（“北大”⊂“北京大学”、“中科院”⊂“中国科学院”）
        if 2 <= len(short) <= 4 and len(long) > len(short):
            if short[0] == long[0] and self._is_subsequence(short, long):
                return True

        return False

    # ==================== 标准名选择 ====================

    @staticmethod
    def choose_canonical(names: List[str], counts: Dict[str, int] = None) -> str:
        """从一组同义名称中选出标准名：优先最长（全称更正式），
        其次出现次数多，最后按字典序保证确定性"""
        counts = counts or {}
        return max(names, key=lambda n: (len(n), counts.get(n, 0), n))

    # ==================== 批量消歧 ====================

    def resolve_batch(self, entities: List[Dict],
                      existing_entities: List[Dict] = None
                      ) -> Dict[Tuple[str, str], str]:
        """对一批实体做同义分组（并查集），返回 {(原始名, 类型): 标准名} 映射。

        entities: 本次解析出的实体 [{'text': ..., 'type': ...}, ...]
        existing_entities: 图谱中已有实体，使新实体能与存量实体归并
        """
        # 候选池：(名称, 类型) -> 出现次数
        pool: Dict[Tuple[str, str], int] = {}
        for e in (existing_entities or []):
            pool.setdefault((e['text'], e['type']), e.get('count', 1))
        for e in entities:
            key = (e['text'], e['type'])
            pool[key] = pool.get(key, 0) + 1

        keys = list(pool.keys())
        parent = {k: k for k in keys}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                (name_a, type_a), (name_b, type_b) = keys[i], keys[j]
                if self.are_synonyms(name_a, name_b, type_a, type_b):
                    union(keys[i], keys[j])

        # 每个同义组选出标准名，生成 原始名 -> 标准名 映射
        groups: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
        for k in keys:
            groups.setdefault(find(k), []).append(k)

        mapping: Dict[Tuple[str, str], str] = {}
        for members in groups.values():
            if len(members) < 2:
                continue
            counts = {name: pool[(name, t)] for name, t in members}
            canonical = self.choose_canonical([name for name, _ in members], counts)
            for name, _ in members:
                if name != canonical:
                    mapping[(name, members[0][1])] = canonical

        return mapping
