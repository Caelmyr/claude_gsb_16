"""
图谱构建模块 - 从文档构建知识图谱
"""
from typing import List, Dict
from backend.nlp.pipeline import NLPPipeline
from backend.graph.storage import GraphStorage


class GraphBuilder:
    """图谱构建器"""

    def __init__(self):
        self.nlp_pipeline = NLPPipeline()
        self.storage = GraphStorage()

    def build_from_text(self, text: str, doc_id: str = None) -> Dict:
        """从文本构建图谱（解析阶段自动识别同义实体并合并）"""
        # NLP处理
        result = self.nlp_pipeline.process(text)

        entities = result['entities']
        relations = result['relations']

        # ===== 实体消歧：识别同义实体，归一到标准名 =====
        # 与图谱已有实体一起分组，使新实体能归并到存量标准实体上
        alias_mapping = self.storage.resolver.resolve_batch(
            entities, self.storage.get_all_entities()
        )

        # 标准名 -> 本次解析出的别名集合
        canonical_aliases: Dict[str, set] = {}
        for entity in entities:
            key = (entity['text'], entity['type'])
            canonical = alias_mapping.get(key)
            if canonical and canonical != entity['text']:
                canonical_aliases.setdefault(canonical, set()).add(entity['text'])
                entity['text'] = canonical

        # 关系两端同步替换为标准名
        for relation in relations:
            s_key = (relation['subject'], relation['subject_type'])
            o_key = (relation['object'], relation['object_type'])
            relation['subject'] = alias_mapping.get(s_key, relation['subject'])
            relation['object'] = alias_mapping.get(o_key, relation['object'])

        # 归一后可能产生重复实体/关系，重新去重
        entities = self._deduplicate_entities(entities)
        relations = self._deduplicate_relations(relations)

        # 添加实体到图谱（携带本次识别出的别名）
        for entity in entities:
            self.storage.add_entity(
                entity['text'],
                entity['type'],
                {
                    'context': entity.get('context', ''),
                    'doc_id': doc_id,
                    'aliases': sorted(canonical_aliases.get(entity['text'], set()))
                }
            )

        # 添加关系到图谱
        for relation in relations:
            self.storage.add_relation(
                relation['subject'],
                relation['subject_type'],
                relation['predicate'],
                relation['object'],
                relation['object_type'],
                {'source_text': relation.get('source_text', ''), 'doc_id': doc_id}
            )

        merged_entities = [
            {'canonical': canonical, 'aliases': sorted(aliases)}
            for canonical, aliases in canonical_aliases.items()
        ]

        return {
            'doc_id': doc_id,
            'entities_count': len(entities),
            'relations_count': len(relations),
            'triples': [(r['subject'], r['predicate'], r['object']) for r in relations],
            'entities': entities,
            'relations': relations,
            'merged_entities': merged_entities
        }

    @staticmethod
    def _deduplicate_entities(entities: List[Dict]) -> List[Dict]:
        """按 (名称, 类型) 去重"""
        seen = {}
        for entity in entities:
            key = (entity['text'], entity['type'])
            if key not in seen:
                seen[key] = entity
        return list(seen.values())

    @staticmethod
    def _deduplicate_relations(relations: List[Dict]) -> List[Dict]:
        """按 (主语, 谓语, 宾语) 去重"""
        seen = {}
        for relation in relations:
            key = (relation['subject'], relation['predicate'], relation['object'])
            if key not in seen:
                seen[key] = relation
        return list(seen.values())

    def build_from_document(self, doc_path: str, doc_id: str) -> Dict:
        """从文档文件构建图谱"""
        from backend.utils.text_extractor import extract_text
        text = extract_text(doc_path)
        return self.build_from_text(text, doc_id)

    def add_triple(self, subject: str, subject_type: str, predicate: str,
                   obj: str, object_type: str) -> Dict:
        """手动添加三元组"""
        self.storage.add_relation(subject, subject_type, predicate, obj, object_type)
        return {'status': 'success', 'triple': (subject, predicate, obj)}

    def get_graph_data(self) -> Dict:
        """获取图谱可视化数据"""
        return self.storage.get_graph_data()

    def get_statistics(self) -> Dict:
        """获取图谱统计"""
        return self.storage.get_statistics()

    def query(self, query_text: str) -> Dict:
        """查询图谱"""
        from backend.graph.query import GraphQuery
        query_engine = GraphQuery(self.storage)
        return query_engine.query_keyword(query_text)
