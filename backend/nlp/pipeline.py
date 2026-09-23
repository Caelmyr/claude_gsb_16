"""
NLP处理管道 - 整合分词、NER、实体消歧、关系抽取
"""
from typing import List, Dict, Set
from backend.nlp.tokenizer import ChineseTokenizer
from backend.nlp.ner import NamedEntityRecognizer
from backend.nlp.relation_extractor import RelationExtractor
from backend.nlp.entity_resolver import EntityResolver


class NLPPipeline:
    """NLP处理管道"""

    def __init__(self):
        self.tokenizer = ChineseTokenizer()
        self.ner = NamedEntityRecognizer()
        self.relation_extractor = RelationExtractor()
        self.resolver = EntityResolver()

    def process(self, text: str) -> Dict:
        """完整处理文本，返回实体和关系"""
        # 1. 分句
        sentences = self.tokenizer.segment_sentences(text)

        # 2. 从全文提取简称/缩写声明（北京大学（以下简称北大）等）
        self.resolver.extract_aliases(text)

        all_entities = []
        all_relations = []
        processed_sentences = []
        surface_names: Dict[str, Set[str]] = {}

        for sentence in sentences:
            if len(sentence) < 5:  # 过短句子跳过
                continue

            # 3. 命名实体识别
            entities = self.ner.recognize_with_context(sentence)

            # 3.1 同义名称补充识别（将“北大”“AI”等简称缩写作为实体注入）
            entities.extend(self.resolver.inject_mentions(sentence, entities))
            entities.sort(key=lambda x: x['start'])

            # 4. 关系抽取
            relations = self.relation_extractor.extract_relations(sentence, entities)

            all_entities.extend(entities)
            all_relations.extend(relations)

            for entity in entities:
                surface_names.setdefault(entity['type'], set()).add(entity['text'])

            processed_sentences.append({
                'text': sentence,
                'entities': entities,
                'relations': relations
            })

        # 5. 同义实体聚类（类型安全的并查集合并）
        self.resolver.build_clusters(surface_names)

        # 6. 实体消歧合并：同义实体归并为一个节点，原始名称保留为别名
        unique_entities = self._deduplicate_entities(all_entities)
        merged_entities = self.resolver.merge_entities(unique_entities)

        # 7. 关系规范化：主语/宾语改写为规范名称并去重、去除自环
        unique_relations = self._deduplicate_relations(all_relations)
        merged_relations = self._canonicalize_relations(unique_relations)

        # 8. 汇总本次解析的消歧结果
        merge_groups = self.resolver.groups
        merged_count = sum(len(g['aliases']) for g in merge_groups)

        return {
            'text': text,
            'sentences': processed_sentences,
            'entities': merged_entities,
            'relations': merged_relations,
            'triples': [(r['subject'], r['predicate'], r['object']) for r in merged_relations],
            'merged_count': merged_count,
            'merge_groups': merge_groups,
            'aliases': self.resolver.aliases_to_register()
        }

    def _deduplicate_entities(self, entities: List[Dict]) -> List[Dict]:
        """实体去重（消歧前的同名同类型去重）"""
        seen = {}
        for entity in entities:
            key = (entity['text'], entity['type'])
            if key not in seen:
                seen[key] = entity
        return list(seen.values())

    def _deduplicate_relations(self, relations: List[Dict]) -> List[Dict]:
        """关系去重"""
        seen = {}
        for relation in relations:
            key = (relation['subject'], relation['predicate'], relation['object'])
            if key not in seen:
                seen[key] = relation
        return list(seen.values())

    def _canonicalize_relations(self, relations: List[Dict]) -> List[Dict]:
        """将关系中的同义实体名称改写为规范名称"""
        canonical_relations: List[Dict] = []
        seen = set()

        for relation in relations:
            subject, subject_type = self.resolver.resolve(
                relation['subject'], relation['subject_type'])
            obj, object_type = self.resolver.resolve(
                relation['object'], relation['object_type'])

            # 合并后产生自环（北大 -属于-> 北京大学 之类），丢弃
            if subject == obj and subject_type == object_type:
                continue

            key = (subject, relation['predicate'], obj)
            if key in seen:
                continue
            seen.add(key)

            canonical_relations.append({
                **relation,
                'subject': subject,
                'subject_type': subject_type,
                'object': obj,
                'object_type': object_type
            })

        return canonical_relations

    def extract_keywords(self, text: str, topk: int = 10) -> List[str]:
        """提取关键词"""
        import jieba.analyse
        return jieba.analyse.extract_tags(text, topK=topk)
