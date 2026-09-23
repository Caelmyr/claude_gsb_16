"""
图谱存储模块 - JSON文件分片存储

实体消歧设计：
- 每个实体节点维护 aliases 字段保存别名列表
- 别名索引（aliases.json）记录 规范化别名 -> 标准实体名
- 写入路径（add_entity）自动识别同义实体并合并节点：
  迁移计数、属性、别名，并重写所有关系中引用的旧名称
- 读取路径（get_entity / get_entity_relations / search_entities）
  自动将别名解析为标准名，搜索任一名称都能定位到合并后的实体
- 启动时对存量图谱执行一次合并清理，兼容历史数据
"""
import json
import os
import threading
from typing import Dict, List, Optional
from backend.utils.config import GRAPH_DIR, GRAPH_SHARDS, ALIASES_FILE
from backend.graph.entity_resolver import EntityResolver


class GraphStorage:
    """图谱存储管理器 - 按实体类型分片"""

    def __init__(self):
        # 可重入锁：add_relation 持锁时会调用 add_entity，
        # 普通 Lock 会导致死锁
        self.lock = threading.RLock()
        self.resolver = EntityResolver()
        self._alias_map: Dict[str, str] = {}  # 规范化别名 -> 标准实体名
        self._ensure_directories()
        self._cache = {}
        self._load_all_shards()
        self._load_aliases()
        # 启动时合并存量图谱中的同义实体（兼容历史数据）
        self._consolidate_entities()

    def _ensure_directories(self):
        """确保目录存在"""
        os.makedirs(GRAPH_DIR, exist_ok=True)

    def _load_all_shards(self):
        """加载所有分片到缓存"""
        for entity_type, filename in GRAPH_SHARDS.items():
            filepath = os.path.join(GRAPH_DIR, filename)
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    self._cache[entity_type] = json.load(f)
            else:
                self._cache[entity_type] = {'entities': {}, 'relations': []}

    def _save_shard(self, entity_type: str):
        """保存指定分片到文件"""
        filename = GRAPH_SHARDS.get(entity_type, 'other.json')
        filepath = os.path.join(GRAPH_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self._cache[entity_type], f, ensure_ascii=False, indent=2)

    # ==================== 别名索引 ====================

    def _load_aliases(self):
        """加载别名索引，并根据实体记录中的别名列表重建（兼容索引文件丢失）"""
        if os.path.exists(ALIASES_FILE):
            with open(ALIASES_FILE, 'r', encoding='utf-8') as f:
                self._alias_map = json.load(f)
        for entity_type, shard in self._cache.items():
            for text, data in shard['entities'].items():
                for alias in data.get('aliases', []):
                    self._alias_map[self.resolver.normalize(alias)] = text

    def _save_aliases(self):
        """持久化别名索引"""
        with open(ALIASES_FILE, 'w', encoding='utf-8') as f:
            json.dump(self._alias_map, f, ensure_ascii=False, indent=2)

    def resolve_name(self, entity_text: str) -> str:
        """将实体名（可能是别名）解析为标准实体名（只读，不触发合并）"""
        norm = self.resolver.normalize(entity_text)
        canonical = self._alias_map.get(norm)
        if canonical:
            return canonical
        # 内置别名词典兜底：如图谱中只有“北京大学”，查询“北大”也能命中
        full = self.resolver.lookup_dictionary(entity_text)
        if full:
            for shard in self._cache.values():
                if full in shard['entities']:
                    return full
        return entity_text

    # ==================== 实体写入与合并 ====================

    def _next_entity_id(self, entity_type: str) -> str:
        """生成实体ID：取当前分片最大序号+1，避免合并删除节点后撞号"""
        max_num = -1
        for data in self._cache.get(entity_type, {}).get('entities', {}).values():
            try:
                num = int(str(data.get('id', '')).rsplit('_', 1)[1])
                max_num = max(max_num, num)
            except (ValueError, IndexError):
                continue
        return f'{entity_type}_{max_num + 1}'

    def add_entity(self, entity_text: str, entity_type: str, properties: Dict = None):
        """添加实体（自动识别同义实体并合并为一个节点，原名称保留为别名）"""
        with self.lock:
            properties = dict(properties or {})
            new_aliases = properties.pop('aliases', None) or []

            if entity_type not in self._cache:
                self._cache[entity_type] = {'entities': {}, 'relations': []}
            shard = self._cache[entity_type]

            # 1. 别名索引解析：已知别名直接归并到标准实体
            mapped = self._alias_map.get(self.resolver.normalize(entity_text))
            if mapped and mapped != entity_text and mapped in shard['entities']:
                entity_text = mapped

            # 2. 启发式同义识别：与现有同类型实体匹配，命中则合并节点
            if entity_text not in shard['entities']:
                synonym = self._find_synonym_entity(entity_text, entity_type)
                if synonym:
                    counts = {
                        entity_text: 1,
                        synonym: shard['entities'][synonym].get('count', 1)
                    }
                    canonical = self.resolver.choose_canonical(
                        [entity_text, synonym], counts)
                    if canonical == entity_text:
                        # 新名称更正式（更长）：旧实体并入新名称
                        self._absorb_entity(synonym, entity_text, entity_type)
                    else:
                        # 现有实体更正式：新名称先落库再整体并入
                        shard['entities'][entity_text] = {
                            'id': self._next_entity_id(entity_type),
                            'text': entity_text,
                            'type': entity_type,
                            'properties': {},
                            'aliases': [],
                            'count': 0
                        }
                        self._absorb_entity(entity_text, synonym, entity_type)
                        entity_text = synonym

            # 3. 常规添加/计数
            if entity_text not in shard['entities']:
                shard['entities'][entity_text] = {
                    'id': self._next_entity_id(entity_type),
                    'text': entity_text,
                    'type': entity_type,
                    'properties': properties,
                    'aliases': [],
                    'count': 1
                }
            else:
                entity = shard['entities'][entity_text]
                entity['count'] = entity.get('count', 0) + 1
                if properties:
                    entity.setdefault('properties', {}).update(properties)

            # 4. 登记新别名到实体和别名索引
            if new_aliases:
                entity = shard['entities'][entity_text]
                aliases = set(entity.get('aliases', []))
                for alias in new_aliases:
                    norm = self.resolver.normalize(alias)
                    if norm and norm != self.resolver.normalize(entity_text):
                        aliases.add(alias)
                        self._alias_map[norm] = entity_text
                entity['aliases'] = sorted(aliases)
                self._save_aliases()

            self._save_shard(entity_type)

    def _find_synonym_entity(self, entity_text: str, entity_type: str) -> Optional[str]:
        """在同类型实体中启发式查找同义实体（含与已有别名比较），返回标准名"""
        shard = self._cache.get(entity_type)
        if not shard:
            return None
        for existing_text, data in shard['entities'].items():
            if existing_text == entity_text:
                continue
            if self.resolver.are_synonyms(entity_text, existing_text,
                                          entity_type, entity_type):
                return existing_text
            for alias in data.get('aliases', []):
                if self.resolver.are_synonyms(entity_text, alias,
                                              entity_type, entity_type):
                    return existing_text
        return None

    def _absorb_entity(self, absorbed_text: str, into_text: str, entity_type: str):
        """把 absorbed 实体合并进 into 实体（同类型）：
        迁移计数、属性、别名，并重写所有关系中引用的旧名称"""
        shard = self._cache.get(entity_type)
        if not shard:
            return
        absorbed = shard['entities'].pop(absorbed_text, None)
        if absorbed is None:
            return

        into = shard['entities'].get(into_text)
        if into is None:
            # 目标实体不存在：直接改名迁移
            absorbed['text'] = into_text
            shard['entities'][into_text] = absorbed
            into = absorbed
        else:
            into['count'] = into.get('count', 1) + absorbed.get('count', 1)
            # 合并属性：保留目标实体属性，补充来源实体独有属性
            into_props = into.setdefault('properties', {})
            for key, value in absorbed.get('properties', {}).items():
                into_props.setdefault(key, value)

        # 归集别名：被吸收实体的名称及其全部别名都转为标准实体的别名
        aliases = set(into.get('aliases', []))
        aliases.update(absorbed.get('aliases', []))
        aliases.add(absorbed_text)
        aliases.discard(into_text)
        into['aliases'] = sorted(aliases)
        for alias in into['aliases']:
            self._alias_map[self.resolver.normalize(alias)] = into_text

        # 重写所有关系中引用的旧实体名
        self._rewrite_relations(absorbed_text, into_text)
        self._save_shard(entity_type)
        self._save_aliases()

    def _rewrite_relations(self, old_text: str, new_text: str):
        """把所有关系中出现的旧实体名替换为标准名，并去除重复关系和自环"""
        for entity_type, shard in self._cache.items():
            changed = False
            for rel in shard['relations']:
                if rel['subject'] == old_text:
                    rel['subject'] = new_text
                    changed = True
                if rel['object'] == old_text:
                    rel['object'] = new_text
                    changed = True
            if changed:
                seen = set()
                unique = []
                for rel in shard['relations']:
                    if rel['subject'] == rel['object']:
                        continue  # 合并产生的自环，丢弃
                    key = (rel['subject'], rel['predicate'], rel['object'])
                    if key not in seen:
                        seen.add(key)
                        unique.append(rel)
                shard['relations'] = unique
                self._save_shard(entity_type)

    def _consolidate_entities(self):
        """启动时合并存量图谱中的同义实体（兼容合并功能上线前的历史数据）"""
        with self.lock:
            for entity_type, shard in self._cache.items():
                texts = list(shard['entities'].keys())
                for i, name_a in enumerate(texts):
                    if name_a not in shard['entities']:
                        continue  # 已被合并
                    for name_b in texts[i + 1:]:
                        if name_b not in shard['entities']:
                            continue
                        if self.resolver.are_synonyms(name_a, name_b,
                                                      entity_type, entity_type):
                            counts = {
                                name_a: shard['entities'][name_a].get('count', 1),
                                name_b: shard['entities'][name_b].get('count', 1)
                            }
                            canonical = self.resolver.choose_canonical(
                                [name_a, name_b], counts)
                            absorbed = name_b if canonical == name_a else name_a
                            print(f'实体合并: {absorbed} -> {canonical}')
                            self._absorb_entity(absorbed, canonical, entity_type)

    # ==================== 别名管理 ====================

    def get_alias_groups(self) -> List[Dict]:
        """获取所有标准实体及其别名列表"""
        groups = []
        for entity_type, shard in self._cache.items():
            for text, data in shard['entities'].items():
                aliases = data.get('aliases', [])
                if aliases:
                    groups.append({
                        'canonical': text,
                        'type': entity_type,
                        'aliases': aliases
                    })
        return groups

    def add_alias(self, alias: str, canonical: str) -> Dict:
        """手动登记别名；若别名对应的实体已存在且同类型，则触发实体合并"""
        with self.lock:
            canonical_entity = self.get_entity(canonical)
            if not canonical_entity:
                return {'success': False, 'error': f'标准实体不存在: {canonical}'}

            canonical_text = canonical_entity['text']
            entity_type = canonical_entity['type']
            if self.resolver.normalize(alias) == self.resolver.normalize(canonical_text):
                return {'success': False, 'error': '别名与标准名相同'}

            shard = self._cache[entity_type]
            if alias in shard['entities']:
                # 别名实体已存在：整体合并到标准实体
                self._absorb_entity(alias, canonical_text, entity_type)
            else:
                entity = shard['entities'][canonical_text]
                aliases = set(entity.get('aliases', []))
                aliases.add(alias)
                entity['aliases'] = sorted(aliases)
                self._alias_map[self.resolver.normalize(alias)] = canonical_text
                self._save_shard(entity_type)
                self._save_aliases()

            return {'success': True, 'canonical': canonical_text, 'alias': alias}

    # ==================== 关系写入 ====================

    def add_relation(self, subject: str, subject_type: str, predicate: str,
                     obj: str, object_type: str, properties: Dict = None):
        """添加关系（实体名会先经别名解析归并到标准实体）"""
        with self.lock:
            # 确保实体存在（add_entity 内部会做同义归并）
            self.add_entity(subject, subject_type)
            self.add_entity(obj, object_type)

            # 归并后实体名可能变化，重新解析为标准名
            subject = self.resolve_name(subject)
            obj = self.resolve_name(obj)

            # 添加关系到主语所在分片
            if subject_type not in self._cache:
                self._cache[subject_type] = {'entities': {}, 'relations': []}

            relation = {
                'subject': subject,
                'subject_type': subject_type,
                'predicate': predicate,
                'object': obj,
                'object_type': object_type,
                'properties': properties or {}
            }

            # 检查是否已存在
            existing = self._cache[subject_type]['relations']
            if not any(r['subject'] == subject and r['predicate'] == predicate and r['object'] == obj for r in existing):
                existing.append(relation)
                self._save_shard(subject_type)

    # ==================== 查询 ====================

    def get_entity(self, entity_text: str) -> Optional[Dict]:
        """获取实体信息（自动将别名解析为标准实体）"""
        canonical = self.resolve_name(entity_text)
        for entity_type, shard in self._cache.items():
            if canonical in shard['entities']:
                return shard['entities'][canonical]
        return None

    def get_entity_relations(self, entity_text: str) -> List[Dict]:
        """获取实体的所有关系（自动将别名解析为标准实体）"""
        canonical = self.resolve_name(entity_text)
        relations = []
        for entity_type, shard in self._cache.items():
            for relation in shard['relations']:
                if relation['subject'] == canonical or relation['object'] == canonical:
                    relations.append(relation)
        return relations

    def get_all_entities(self) -> List[Dict]:
        """获取所有实体"""
        entities = []
        for entity_type, shard in self._cache.items():
            entities.extend(shard['entities'].values())
        return entities

    def get_all_relations(self) -> List[Dict]:
        """获取所有关系"""
        relations = []
        for entity_type, shard in self._cache.items():
            relations.extend(shard['relations'])
        return relations

    def get_graph_data(self) -> Dict:
        """获取图谱可视化数据（节点携带别名，供前端展示和搜索）"""
        nodes = []
        links = []
        node_ids = set()

        for entity_type, shard in self._cache.items():
            for entity_text, entity_data in shard['entities'].items():
                if entity_data['id'] not in node_ids:
                    node_ids.add(entity_data['id'])
                    nodes.append({
                        'id': entity_data['id'],
                        'label': entity_text,
                        'type': entity_type,
                        'count': entity_data.get('count', 1),
                        'aliases': entity_data.get('aliases', [])
                    })

            for relation in shard['relations']:
                source_entity = self.get_entity(relation['subject'])
                target_entity = self.get_entity(relation['object'])
                if source_entity and target_entity:
                    links.append({
                        'source': source_entity['id'],
                        'target': target_entity['id'],
                        'label': relation['predicate']
                    })

        return {'nodes': nodes, 'links': links}

    def search_entities(self, keyword: str) -> List[Dict]:
        """搜索实体：同时匹配标准名和别名，搜索任一名称都能定位到合并后的实体"""
        results = []
        seen_ids = set()
        resolved = self.resolve_name(keyword)  # 关键词本身是别名时直接命中标准实体

        for entity_type, shard in self._cache.items():
            for entity_text, entity_data in shard['entities'].items():
                names = [entity_text] + entity_data.get('aliases', [])
                matched = any(keyword in name for name in names)
                if not matched and entity_text == resolved:
                    matched = True
                if matched and entity_data['id'] not in seen_ids:
                    seen_ids.add(entity_data['id'])
                    results.append(entity_data)
        return results

    def get_statistics(self) -> Dict:
        """获取图谱统计信息"""
        total_entities = 0
        total_relations = 0
        entity_counts = {}

        for entity_type, shard in self._cache.items():
            count = len(shard['entities'])
            entity_counts[entity_type] = count
            total_entities += count
            total_relations += len(shard['relations'])

        return {
            'total_entities': total_entities,
            'total_relations': total_relations,
            'entity_counts': entity_counts
        }
