"""
图谱构建模块 - 从文档构建知识图谱
"""
from typing import List, Dict
from backend.nlp.pipeline import NLPPipeline
from backend.graph.storage import GraphStorage


class GraphBuilder:
    """图谱构建器"""

    def __init__(self, storage: GraphStorage = None):
        self.nlp_pipeline = NLPPipeline()
        # 复用外部共享存储，避免多个存储实例缓存不一致
        self.storage = storage or GraphStorage()

    def build_from_text(self, text: str, doc_id: str = None) -> Dict:
        """从文本构建图谱（解析阶段自动完成同义实体识别与合并）"""
        # NLP处理（含实体消歧）
        result = self.nlp_pipeline.process(text)

        # 先登记本次解析发现的同义名称，保证后续写入即重定向到规范实体
        # （包括别名本身未被NER识别的情形，如“麻省理工学院（MIT）”中的MIT）
        self.storage.register_aliases(result.get('aliases', []))

        # 添加实体到图谱（已是合并后的规范实体，原始名称保存在 aliases 字段）
        for entity in result['entities']:
            self.storage.add_entity(
                entity['text'],
                entity['type'],
                {
                    'context': entity.get('context', ''),
                    'doc_id': doc_id,
                    'aliases': entity.get('aliases', [])
                }
            )

        # 添加关系到图谱（主语/宾语已是规范名称）
        for relation in result['relations']:
            self.storage.add_relation(
                relation['subject'],
                relation['subject_type'],
                relation['predicate'],
                relation['object'],
                relation['object_type'],
                {'source_text': relation.get('source_text', ''), 'doc_id': doc_id}
            )

        return {
            'doc_id': doc_id,
            'entities_count': len(result['entities']),
            'relations_count': len(result['relations']),
            'triples': result['triples'],
            'entities': result['entities'],
            'relations': result['relations'],
            'merged_count': result.get('merged_count', 0),
            'merge_groups': result.get('merge_groups', [])
        }

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
