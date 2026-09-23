"""
实体消歧（Entity Resolution）模块

在解析阶段识别指向同一现实实体的不同名称（全称、简称、英文缩写等），
将同义实体合并为同一个节点：
- 规范名称（canonical name）作为节点显示名
- 所有原始名称保留为别名（aliases）
- 搜索任一名称均可定位到合并后的实体

两类同义线索：
1. 内置同义词典 BUILTIN_SYNONYMS：常见高校、科研机构、企业、地名和科技术语
2. 文本中的简称/缩写声明，例如：
   北京大学（以下简称“北大”）
   中国科学院（简称中科院）
   麻省理工学院（MIT）
   人工智能（Artificial Intelligence，简称AI）
   XX研究院，以下简称XX院

合并只在相同实体类型之间进行，避免跨类型误合（如“中国”不会与“中科院”合并）。
"""
import re
from typing import Dict, List, Optional, Set, Tuple

# 内置同义实体组：(规范名称, 实体类型, [别名...])
BUILTIN_SYNONYMS: List[Tuple[str, str, List[str]]] = [
    # —— 高校 ——
    ('北京大学', 'ORG', ['北大', 'Peking University', 'PKU']),
    ('清华大学', 'ORG', ['清华', 'Tsinghua University', 'THU']),
    ('中国人民大学', 'ORG', ['人大', 'RUC']),
    ('北京师范大学', 'ORG', ['北师大', 'BNU']),
    ('北京航空航天大学', 'ORG', ['北航', 'BUAA']),
    ('北京理工大学', 'ORG', ['北理工', 'BIT']),
    ('复旦大学', 'ORG', ['复旦', 'Fudan University']),
    ('上海交通大学', 'ORG', ['上海交大', 'SJTU']),
    ('西安交通大学', 'ORG', ['西安交大', 'XJTU']),
    ('浙江大学', 'ORG', ['浙大', 'ZJU']),
    ('南京大学', 'ORG', ['南大', 'NJU']),
    ('南开大学', 'ORG', ['南开', 'NKU']),
    ('武汉大学', 'ORG', ['武大', 'WHU']),
    ('哈尔滨工业大学', 'ORG', ['哈工大', 'HIT']),
    ('麻省理工学院', 'ORG', ['麻省理工', 'MIT']),
    ('斯坦福大学', 'ORG', ['斯坦福', 'Stanford University', 'Stanford']),
    # —— 科研机构 ——
    ('中国科学院', 'ORG', ['中科院', 'Chinese Academy of Sciences', 'CAS']),
    ('中国社会科学院', 'ORG', ['社科院', 'CASS']),
    ('中国工程院', 'ORG', ['工程院', 'CAE']),
    # —— 企业 ——
    ('谷歌公司', 'ORG', ['谷歌', 'Google']),
    ('微软公司', 'ORG', ['微软', 'Microsoft']),
    ('Meta公司', 'ORG', ['Meta', 'Facebook']),
    ('OpenAI公司', 'ORG', ['OpenAI']),
    ('特斯拉公司', 'ORG', ['特斯拉', 'Tesla']),
    ('蚂蚁集团', 'ORG', ['蚂蚁金服']),
    ('阿里巴巴集团', 'ORG', ['阿里巴巴', 'Alibaba']),
    # —— 地名 ——
    ('中国', 'LOCATION', ['中华人民共和国']),
    ('北京', 'LOCATION', ['北京市']),
    ('上海', 'LOCATION', ['上海市']),
    ('广州', 'LOCATION', ['广州市']),
    ('深圳', 'LOCATION', ['深圳市']),
    ('杭州', 'LOCATION', ['杭州市']),
    ('南京', 'LOCATION', ['南京市']),
    ('成都', 'LOCATION', ['成都市']),
    ('武汉', 'LOCATION', ['武汉市']),
    ('西安', 'LOCATION', ['西安市']),
    # —— 科技术语 ——
    ('人工智能', 'CONCEPT', ['AI', 'Artificial Intelligence']),
    ('机器学习', 'CONCEPT', ['ML', 'Machine Learning']),
    ('深度学习', 'CONCEPT', ['Deep Learning']),
    ('自然语言处理', 'CONCEPT', ['NLP']),
    ('卷积神经网络', 'CONCEPT', ['CNN']),
    ('循环神经网络', 'CONCEPT', ['RNN']),
    ('大语言模型', 'CONCEPT', ['LLM']),
]

# “全称（括号内说明）”形式
_PAREN_RE = re.compile(
    r'([一-龥A-Za-z0-9][一-龥A-Za-z0-9 ·&]{1,19})\s*[（(]([^）)]{1,40})[）)]'
)
# 括号内的显式简称标记，如（以下简称北大）、(简称AI)
_MARKER_IN_PAREN_RE = re.compile(
    r'(?:以下简称为|以下简称|下称为|下称|简称为|简称|缩写为)\s*[“"]?([一-龥A-Za-z0-9]{1,12})[”"]?'
)
# “全称，以下简称别名”形式
_STANDALONE_RE = re.compile(
    r'[，,；;\n]\s*'
    r'([一-龥A-Za-z0-9][一-龥A-Za-z0-9 ·&]{1,24}?)\s*[，,]\s*'
    r'(?:以下简称为|以下简称|简称为|简称|缩写为)\s*[“"]?([一-龥A-Za-z0-9]{1,12})[”"]?'
)
_CJK_RE = re.compile(r'^[一-龥]+$')
_LATIN_ACRONYM_RE = re.compile(r'^(?=.*[A-Z])[A-Za-z0-9][A-Za-z0-9 .&/\-]{1,14}$')


class EntityResolver:
    """同义实体解析器：识别别名并把实体归并到规范名称"""

    def __init__(self):
        # (全称, 别名, 类型|None)
        self._pairs: List[Tuple[str, str, Optional[str]]] = []
        # 内置规范名 -> 类型
        self._designated: Dict[str, str] = {}
        # 已知名称 -> 类型（用于在NER阶段补充识别简称/缩写）
        self._known_names: Dict[str, str] = {}
        # build_clusters 后的解析结果：(原名, 类型) -> (规范名, 类型)
        self._canonical_map: Dict[Tuple[str, str], Tuple[str, str]] = {}
        # 合并分组信息
        self._groups: List[Dict] = []
        self._load_builtins()

    def _load_builtins(self):
        for canonical, entity_type, aliases in BUILTIN_SYNONYMS:
            self._designated[canonical] = entity_type
            self._known_names[canonical] = entity_type
            for alias in aliases:
                self._known_names[alias] = entity_type
                self._pairs.append((canonical, alias, entity_type))

    @property
    def groups(self) -> List[Dict]:
        return self._groups

    def known_names(self) -> Dict[str, str]:
        """返回已知同义名称及其类型（全称+别名），供NER补充识别"""
        return dict(self._known_names)

    def add_alias(self, full_name: str, alias: str,
                  entity_type: Optional[str] = None):
        """登记一对同义名称"""
        full_name = (full_name or '').strip()
        alias = (alias or '').strip().strip('“”"\'')
        if not full_name or not alias or full_name == alias:
            return
        self._pairs.append((full_name, alias, entity_type))
        if entity_type:
            self._known_names.setdefault(full_name, entity_type)
            self._known_names.setdefault(alias, entity_type)

    # ------------------------------------------------------------------
    # 从文本中提取简称/缩写声明
    # ------------------------------------------------------------------

    def extract_aliases(self, text: str) -> List[Tuple[str, str]]:
        """扫描文本中的简称/缩写声明，登记同义对并返回"""
        found: List[Tuple[str, str]] = []

        def register(full: str, alias: str):
            full, alias = full.strip(), alias.strip().strip('“”"\'')
            if not self._valid_pair(full, alias):
                return
            self.add_alias(full, alias)
            found.append((full, alias))

        # 全称（说明）
        for match in _PAREN_RE.finditer(text):
            full, inner = match.group(1).strip(), match.group(2).strip()
            marker = _MARKER_IN_PAREN_RE.search(inner)
            if marker:
                register(full, marker.group(1))
            elif self._is_direct_alias(full, inner):
                register(full, inner)

        # 全称，以下简称别名
        for match in _STANDALONE_RE.finditer(text):
            register(match.group(1), match.group(2))

        return found

    @staticmethod
    def _valid_pair(full: str, alias: str) -> bool:
        if not full or not alias or full == alias:
            return False
        if len(alias) > len(full) and _CJK_RE.match(alias):
            return False
        return True

    @staticmethod
    def _is_direct_alias(full: str, alias: str) -> bool:
        """判断括号内内容是否可直接视为全称的别名"""
        if alias in full or full in alias:
            return False
        # 中文简称：2~4字，且每个字都来自全称（如 北大<-北京大学、中科院<-中国科学院）
        if _CJK_RE.match(alias):
            if not (2 <= len(alias) <= 4) or len(alias) >= len(full):
                return False
            return all(ch in full for ch in alias)
        # 英文缩写：含大写字母的字母数字串（如 MIT、CNN、GPT-4）
        if _LATIN_ACRONYM_RE.match(alias):
            return True
        return False

    # ------------------------------------------------------------------
    # NER 补充识别：把已知简称/缩写作为实体加入
    # ------------------------------------------------------------------

    def inject_mentions(self, sentence: str,
                        entities: List[Dict]) -> List[Dict]:
        """在句子中扫描已知同义名称，对未被NER覆盖（且不与现有实体重叠）的名称补充实体"""
        occupied: List[Tuple[int, int]] = [(e['start'], e['end']) for e in entities]
        injected: List[Dict] = []
        # 长名称优先，避免“中国”抢占“中国科学院”的片段
        for name in sorted(self._known_names, key=len, reverse=True):
            entity_type = self._known_names[name]
            start = sentence.find(name)
            while start != -1:
                end = start + len(name)
                if not any(start < e_end and end > e_start
                           for e_start, e_end in occupied):
                    injected.append({
                        'text': name,
                        'type': entity_type,
                        'start': start,
                        'end': end,
                        'context': sentence,
                        'source': 'alias'
                    })
                    occupied.append((start, end))
                start = sentence.find(name, start + 1)
        return injected

    # ------------------------------------------------------------------
    # 同义实体聚类与合并
    # ------------------------------------------------------------------

    def build_clusters(self, names_by_type: Dict[str, Set[str]]):
        """基于已登记的同义对，在各实体类型内部做并查集聚类"""
        self._canonical_map = {}
        self._groups = []

        for entity_type, names in names_by_type.items():
            parent = {name: name for name in names}

            def find(x: str) -> str:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a: str, b: str):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

            for a, b, pair_type in self._pairs:
                # 类型必须一致才合并（未标注类型的文本线索要求两端实际同类型）
                if pair_type is not None and pair_type != entity_type:
                    continue
                if a in parent and b in parent:
                    union(a, b)

            clusters: Dict[str, List[str]] = {}
            for name in names:
                clusters.setdefault(find(name), []).append(name)

            for members in clusters.values():
                canonical = self._choose_canonical(members, entity_type)
                aliases = sorted(m for m in members if m != canonical)
                for member in members:
                    self._canonical_map[(member, entity_type)] = (canonical, entity_type)
                if aliases:
                    self._groups.append({
                        'canonical': canonical,
                        'type': entity_type,
                        'aliases': aliases
                    })

    def _choose_canonical(self, members: List[str], entity_type: str) -> str:
        """选择规范名称：内置指定全称 > 最长名称 > 字典序"""
        for member in members:
            if self._designated.get(member) == entity_type:
                return member
        return sorted(members, key=lambda m: (-len(m), m))[0]

    def resolve(self, name: str, entity_type: str) -> Tuple[str, str]:
        """把实体表面名称解析为（规范名称, 类型）"""
        return self._canonical_map.get((name, entity_type), (name, entity_type))

    def merge_entities(self, entities: List[Dict]) -> List[Dict]:
        """将同义实体合并为规范实体，原始名称写入 aliases"""
        merged: Dict[Tuple[str, str], Dict] = {}
        order: List[Tuple[str, str]] = []

        for entity in entities:
            canonical, ctype = self.resolve(entity['text'], entity['type'])
            key = (canonical, ctype)
            if key not in merged:
                canonical_entity = dict(entity)
                canonical_entity['text'] = canonical
                canonical_entity['type'] = ctype
                canonical_entity['aliases'] = []
                merged[key] = canonical_entity
                order.append(key)
            else:
                target = merged[key]
                target['aliases'].append(entity['text'])
                # 保留更长的上下文
                if len(entity.get('context', '')) > len(target.get('context', '')):
                    target['context'] = entity.get('context', '')

        result: List[Dict] = []
        for key in order:
            entity = merged[key]
            canonical, ctype = key
            # 用聚类分组中的完整别名集合（去重、去规范名）
            group = next((g for g in self._groups
                          if g['canonical'] == canonical and g['type'] == ctype), None)
            if group:
                entity['aliases'] = list(group['aliases'])
            else:
                entity['aliases'] = sorted(set(
                    a for a in entity['aliases'] if a != canonical
                ))
            result.append(entity)
        return result

    def aliases_to_register(self) -> List[Tuple[str, str, Optional[str]]]:
        """返回需要写入存储层别名索引的同义对（规范名, 别名, 类型）"""
        pairs: List[Tuple[str, str, Optional[str]]] = []
        seen: Set[Tuple[str, str]] = set()

        # 本次解析中实际发生合并的分组（带类型）
        for group in self._groups:
            for alias in group['aliases']:
                key = (group['canonical'], alias)
                if key not in seen:
                    seen.add(key)
                    pairs.append((group['canonical'], alias, group['type']))

        # 文本中声明、但别名本身未被识别为实体的同义对（类型未知）
        for full, alias, pair_type in self._pairs:
            if pair_type is not None:
                continue
            key = (full, alias)
            if key not in seen:
                seen.add(key)
                pairs.append((full, alias, None))

        return pairs
