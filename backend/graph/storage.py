"""
图谱存储模块 - JSON文件分片存储 + 同义实体别名索引

实体消歧合并的存储层支持：
- aliases.json 维护全局别名索引（别名 -> 规范实体，按类型隔离）
- 新写入的实体/关系自动重定向到规范实体
- 启动时对已存量的同义节点做一次性合并（关系随之改写、重新分片）
- 按别名或规范名均可检索到同一节点
"""
import json
import os
import threading
from typing import Dict, List, Optional, Tuple
from backend.utils.config import GRAPH_DIR, GRAPH_SHARDS

ALIAS_INDEX_FILE = 'aliases.json'


class GraphStorage:
    """图谱存储管理器 - 按实体类型分片"""

    def __init__(self):
        self.lock = threading.RLock()
        self._ensure_directories()
        self._cache = {}
        self._alias_index = {'entries': {}, 'parent': {}}
        self._designated_names = set()
        self._load_all_shards()
        self._load_alias_index()
        # 内置同义词典写入别名索引
        self._seed_builtin_aliases()
        # 合并存量同义节点（幂等）
        merged = self._merge_existing_synonyms()
        if merged:
            for shard_name in GRAPH_SHARDS:
                self._save_shard(shard_name)

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------

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

    def _load_alias_index(self):
        """加载别名索引"""
        filepath = os.path.join(GRAPH_DIR, ALIAS_INDEX_FILE)
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._alias_index['entries'] = data.get('entries', {})
                self._alias_index['parent'] = data.get('parent', {})
            except (json.JSONDecodeError, ValueError):
                pass

    def _save_shard(self, entity_type: str):
        """保存指定分片到文件"""
        if entity_type not in GRAPH_SHARDS:
            return
        filename = GRAPH_SHARDS[entity_type]
        filepath = os.path.join(GRAPH_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self._cache[entity_type], f, ensure_ascii=False, indent=2)

    def _save_alias_index(self):
        """保存别名索引"""
        filepath = os.path.join(GRAPH_DIR, ALIAS_INDEX_FILE)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self._alias_index, f, ensure_ascii=False, indent=2)

    def _seed_builtin_aliases(self):
        """启动时将内置同义词典登记到别名索引"""
        from backend.nlp.entity_resolver import BUILTIN_SYNONYMS

        changed = False
        with self.lock:
            for canonical, entity_type, aliases in BUILTIN_SYNONYMS:
                self._designated_names.add(canonical)
                if self._register_alias_pair(canonical, canonical, entity_type):
                    changed = True
                for alias in aliases:
                    if self._register_alias_pair(canonical, alias, entity_type):
                        changed = True
            if changed:
                self._save_alias_index()

    # ------------------------------------------------------------------
    # 别名索引：并查集维护同义关系
    # ------------------------------------------------------------------

    def _find_root(self, name: str) -> str:
        parent = self._alias_index['parent']
        if name not in parent:
            parent[name] = name
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def _register_alias_pair(self, canonical: str, alias: str,
                             entity_type: Optional[str] = None) -> bool:
        """登记一对同义名称（调用方持锁）。返回索引是否发生变化"""
        entries = self._alias_index['entries']
        changed = False

        if canonical not in entries:
            entries[canonical] = {'canonical': canonical, 'type': entity_type}
            changed = True
        elif entity_type and not entries[canonical].get('type'):
            entries[canonical]['type'] = entity_type
            changed = True

        if alias != canonical and alias not in entries:
            entries[alias] = {'canonical': canonical, 'type': entity_type}
            changed = True

        # 类型传播
        if entity_type:
            root = self._find_root(canonical)
            for node in self._component_members(root):
                entry = entries.get(node)
                if entry and not entry.get('type'):
                    entry['type'] = entity_type
                    changed = True

        ra = self._find_root(canonical)
        rb = self._find_root(alias)
        if ra != rb:
            # 选择更适合作为规范名称的一方作为根：
            # 指定全称 > 组件成员更多 > 名称更长
            winner, loser = self._prefer_root(ra, rb)
            self._alias_index['parent'][loser] = winner
            entries.setdefault(winner, {'canonical': winner, 'type': entity_type})
            entries.setdefault(loser, {'canonical': winner, 'type': entity_type})
            changed = True
        return changed

    def _prefer_root(self, a: str, b: str) -> Tuple[str, str]:
        """在两个并查集根中选择规范名称一方"""
        def score(root: str):
            members = self._component_members(root)
            has_designated = any(m in self._designated_names for m in members)
            return (
                1 if has_designated else 0,
                len(members),
                max((len(m) for m in members), default=len(root))
            )

        if score(a) >= score(b):
            return a, b
        return b, a

    def _component_members(self, root: str) -> List[str]:
        """并查集某个根下的全部成员（调用方持锁）"""
        parent = self._alias_index['parent']
        return [name for name in parent
                if self._find_root(name) == root]

    def register_aliases(self, pairs: List[Tuple[str, str, Optional[str]]]):
        """批量登记同义对 (规范名, 别名, 类型)，并即时合并存量节点"""
        changed = False
        with self.lock:
            for canonical, alias, entity_type in pairs:
                if self._register_alias_pair(canonical, alias, entity_type):
                    changed = True
            if changed:
                self._save_alias_index()
                merged = self._merge_existing_synonyms()
                if merged:
                    for shard_name in set(self._cache.keys()):
                        self._save_shard(shard_name)

    def resolve_name(self, name: str,
                     entity_type: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """把任意表面名称（别名）解析为（规范名称, 类型）；非别名原样返回"""
        with self.lock:
            entries = self._alias_index['entries']
            if name not in entries:
                return name, entity_type
            root = self._find_root(name)
            entry = entries.get(root)
            root_type = entry.get('type') if entry else None
            # 类型冲突时不做重定向（同名不同类实体）
            if entity_type and root_type and root_type != entity_type:
                return name, entity_type
            return root, entity_type or root_type

    def _alias_names(self, canonical: str,
                     entity_type: Optional[str] = None) -> List[str]:
        """规范实体在别名索引中的全部别名（调用方持锁）"""
        if canonical not in self._alias_index['entries']:
            return []
        root = self._find_root(canonical)
        members = self._component_members(root)
        return sorted(m for m in members
                      if m != canonical and self._entry_type_matches(m, entity_type))

    def _entry_type_matches(self, name: str,
                            entity_type: Optional[str]) -> bool:
        if not entity_type:
            return True
        entry_type = self._alias_index['entries'].get(name, {}).get('type')
        return not entry_type or entry_type == entity_type

    # ------------------------------------------------------------------
    # 实体 / 关系写入（自动重定向到规范实体）
    # ------------------------------------------------------------------

    def _new_entity_id(self, entity_type: str) -> str:
        """生成分片内唯一的实体ID"""
        existing = {e['id'] for e in self._cache[entity_type]['entities'].values()}
        index = len(existing)
        while f"{entity_type}_{index}" in existing:
            index += 1
        return f"{entity_type}_{index}"

    def add_entity(self, entity_text: str, entity_type: str, properties: Dict = None):
        """添加实体（同义名称自动归并到规范实体，原始名称保留为别名）"""
        with self.lock:
            entity_type, canonical = self._ensure_entity(
                entity_text, entity_type, properties, count=1)
            self._save_shard(entity_type)
            return canonical

    def _ensure_entity(self, entity_text: str, entity_type: str,
                       properties: Dict = None, count: int = 0):
        """确保实体存在（调用方持锁）；count>0 时累加出现次数，count=0 仅占位不计数"""
        canonical, ctype = self.resolve_name(entity_text, entity_type)
        entity_type = ctype or entity_type

        if entity_type not in self._cache:
            self._cache[entity_type] = {'entities': {}, 'relations': []}

        entities = self._cache[entity_type]['entities']
        if canonical not in entities:
            entities[canonical] = {
                'id': self._new_entity_id(entity_type),
                'text': canonical,
                'type': entity_type,
                'aliases': self._alias_names(canonical, entity_type),
                'properties': properties or {},
                'count': count
            }
        else:
            record = entities[canonical]
            record['count'] = record.get('count', 0) + count
            if properties:
                record.setdefault('properties', {}).update(properties)

        # 别名列表与索引保持同步
        entities[canonical]['aliases'] = self._alias_names(canonical, entity_type)
        return entity_type, canonical

    def add_relation(self, subject: str, subject_type: str, predicate: str,
                     obj: str, object_type: str, properties: Dict = None):
        """添加关系（同义实体自动改写为规范名称）"""
        with self.lock:
            subject, subject_type = self.resolve_name(subject, subject_type)
            obj, object_type = self.resolve_name(obj, object_type)
            subject_type = subject_type or 'OTHER'
            object_type = object_type or 'OTHER'

            # 确保实体存在（关系隐含的实体不增加出现计数）
            self._ensure_entity(subject, subject_type)
            self._ensure_entity(obj, object_type)

            # 合并后指向同一实体的自环关系不写入
            if subject == obj and subject_type == object_type:
                return

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

            existing = self._cache[subject_type]['relations']
            if not any(r['subject'] == subject and r['predicate'] == predicate
                       and r['object'] == obj for r in existing):
                existing.append(relation)
                self._save_shard(subject_type)

    # ------------------------------------------------------------------
    # 存量数据合并（启动迁移）
    # ------------------------------------------------------------------

    def _find_entity_record(self, name: str) -> Optional[Tuple[str, Dict]]:
        """在所有分片中查找实体记录，返回 (分片类型, 记录)"""
        for shard_type, shard in self._cache.items():
            if name in shard['entities']:
                return shard_type, shard['entities'][name]
        return None

    def _merge_existing_synonyms(self) -> int:
        """
        将已存量的同义节点合并为规范节点（幂等）：
        - 实体属性/计数合并到规范节点，原始名称进入 aliases
        - 全部关系按别名索引改写为规范名称并重新分片、去重
        返回合并掉的节点数
        """
        with self.lock:
            entries = self._alias_index['entries']

            # 按类型收集别名组件
            components: Dict[str, Dict[str, List[str]]] = {}
            for name in list(entries.keys()):
                root = self._find_root(name)
                entry_type = entries.get(root, {}).get('type')
                if not entry_type:
                    continue  # 类型未知，不做自动合并
                components.setdefault(entry_type, {}).setdefault(root, []).append(name)

            merged_away = 0
            redirect: Dict[Tuple[str, str], Tuple[str, str]] = {}

            for entity_type, roots in components.items():
                for root, members in roots.items():
                    locations = {m: loc for m in members
                                 if (loc := self._find_entity_record(m))}
                    if not locations:
                        continue

                    target_type = entity_type
                    target_shard = self._cache.setdefault(
                        target_type, {'entities': {}, 'relations': []})

                    # 规范节点：根名称（注册时全称一方为根）
                    canonical = root
                    if canonical in locations:
                        loc_type, target = locations[canonical]
                        if loc_type != target_type:
                            # 规范节点位于错误分片，迁移
                            target_shard['entities'][canonical] = target
                            del self._cache[loc_type]['entities'][canonical]
                            target['type'] = target_type
                    elif canonical in target_shard['entities']:
                        target = target_shard['entities'][canonical]
                    else:
                        # 以某个别名记录为基础重命名为规范名
                        src_name, (src_type, src_record) = next(iter(locations.items()))
                        target = dict(src_record)
                        target['text'] = canonical
                        target['type'] = target_type
                        target['id'] = src_record.get('id') or self._new_entity_id(target_type)
                        target_shard['entities'][canonical] = target
                        del self._cache[src_type]['entities'][src_name]
                        locations.pop(src_name, None)
                        merged_away += 1

                    # 合入其余同义记录
                    for member in list(locations.keys()):
                        if member == canonical:
                            continue
                        loc_type, record = locations[member]
                        if record is target:
                            continue
                        target['count'] = target.get('count', 0) + record.get('count', 0)
                        props = record.get('properties') or {}
                        target.setdefault('properties', {}).update(props)
                        if loc_type in self._cache and member in self._cache[loc_type]['entities']:
                            del self._cache[loc_type]['entities'][member]
                        redirect[(member, loc_type)] = (canonical, target_type)
                        merged_away += 1

                    target['text'] = canonical
                    target['type'] = target_type
                    target.setdefault('aliases', [])
                    alias_names = sorted(
                        m for m in members
                        if m != canonical and self._entry_type_matches(m, target_type))
                    target['aliases'] = sorted(set(target['aliases']) | set(alias_names))

            # 全局改写关系并重新分片
            all_relations: List[Dict] = []
            for shard in self._cache.values():
                all_relations.extend(shard.pop('relations', []))
                shard['relations'] = []

            seen_triples = set()
            for rel in all_relations:
                subj, stype = self.resolve_name(rel['subject'], rel.get('subject_type'))
                obj, otype = self.resolve_name(rel['object'], rel.get('object_type'))
                stype = stype or 'OTHER'
                otype = otype or 'OTHER'
                if subj == obj and stype == otype:
                    continue  # 合并后产生的自环
                key = (subj, rel['predicate'], obj)
                if key in seen_triples:
                    continue
                seen_triples.add(key)

                rel.update({
                    'subject': subj,
                    'subject_type': stype,
                    'object': obj,
                    'object_type': otype
                })
                self._cache.setdefault(stype, {'entities': {}, 'relations': []})
                self._cache[stype]['relations'].append(rel)

            return merged_away

    # ------------------------------------------------------------------
    # 查询（别名透明解析）
    # ------------------------------------------------------------------

    def get_entity(self, entity_text: str,
                   entity_type: Optional[str] = None) -> Optional[Dict]:
        """获取实体信息（按别名或规范名均可）"""
        with self.lock:
            canonical, ctype = self.resolve_name(entity_text, entity_type)
            # 优先在重定向后的类型分片查找
            if ctype and ctype in self._cache and canonical in self._cache[ctype]['entities']:
                return self._cache[ctype]['entities'][canonical]
            for shard in self._cache.values():
                if canonical in shard['entities']:
                    return shard['entities'][canonical]
            return None

    def get_entity_relations(self, entity_text: str) -> List[Dict]:
        """获取实体的所有关系（输入别名等价于输入规范名）"""
        with self.lock:
            canonical, _ = self.resolve_name(entity_text)
            relations = []
            for shard in self._cache.values():
                for relation in shard['relations']:
                    if relation['subject'] == canonical or relation['object'] == canonical:
                        relations.append(relation)
            return relations

    def get_all_entities(self) -> List[Dict]:
        """获取所有实体"""
        with self.lock:
            entities = []
            for shard in self._cache.values():
                entities.extend(shard['entities'].values())
            return entities

    def get_all_relations(self) -> List[Dict]:
        """获取所有关系"""
        with self.lock:
            relations = []
            for shard in self._cache.values():
                relations.extend(shard['relations'])
            return relations

    def get_graph_data(self) -> Dict:
        """获取图谱可视化数据"""
        with self.lock:
            nodes = []
            links = []
            node_ids = set()

            for shard in self._cache.values():
                for entity_data in shard['entities'].values():
                    if entity_data['id'] not in node_ids:
                        node_ids.add(entity_data['id'])
                        nodes.append({
                            'id': entity_data['id'],
                            'label': entity_data['text'],
                            'type': entity_data['type'],
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
        """搜索实体：命中规范名或任一并名均可定位到合并后的实体"""
        with self.lock:
            results = []
            for shard in self._cache.values():
                for entity_text, entity_data in shard['entities'].items():
                    matched_alias = None
                    if keyword in entity_text:
                        matched_alias = entity_text
                    else:
                        for alias in entity_data.get('aliases', []):
                            if keyword in alias:
                                matched_alias = alias
                                break
                    if matched_alias:
                        record = dict(entity_data)
                        record['matched_alias'] = matched_alias
                        results.append(record)
            return results

    def get_statistics(self) -> Dict:
        """获取图谱统计信息"""
        with self.lock:
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
