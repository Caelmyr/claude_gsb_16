"""
知识图谱问答系统 - 配置模块
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, 'data')

# 数据存储路径
DOCUMENTS_DIR = os.path.join(DATA_DIR, 'documents')
TRIPLES_DIR = os.path.join(DATA_DIR, 'triples')
GRAPH_DIR = os.path.join(DATA_DIR, 'graph')
SESSIONS_DIR = os.path.join(DATA_DIR, 'sessions')

# NLP配置
ENTITY_TYPES = ['PERSON', 'ORG', 'LOCATION', 'TIME', 'CONCEPT', 'EVENT', 'OTHER']
RELATION_TYPES = ['属于', '位于', '参与', '包含', '相关', '导致', '使用', '创建', '属于']

# 图谱分片配置
GRAPH_SHARDS = {
    'PERSON': 'person.json',
    'ORG': 'org.json',
    'LOCATION': 'location.json',
    'TIME': 'time.json',
    'CONCEPT': 'concept.json',
    'EVENT': 'event.json',
    'OTHER': 'other.json'
}

# 实体消歧配置
ALIASES_FILE = os.path.join(GRAPH_DIR, 'aliases.json')

# 内置同义词表（简称/别名 -> 标准全称），可按需扩展
BUILTIN_ALIASES = {
    # 高校简称
    '北大': '北京大学',
    '清华': '清华大学',
    '复旦': '复旦大学',
    '交大': '上海交通大学',
    '浙大': '浙江大学',
    '南大': '南京大学',
    '中科大': '中国科学技术大学',
    '华科': '华中科技大学',
    '武大': '武汉大学',
    '中大': '中山大学',
    '川大': '四川大学',
    '哈工大': '哈尔滨工业大学',
    '人大': '中国人民大学',
    '北师大': '北京师范大学',
    # 科研机构简称
    '中科院': '中国科学院',
    '社科院': '中国社会科学院',
    '工程院': '中国工程院',
    # 企业简称
    '阿里': '阿里巴巴',
    '字节': '字节跳动',
}

# 机构名后缀（按长度降序，用于核心名匹配，如“腾讯”vs“腾讯公司”）
ORG_SUFFIXES = [
    '股份有限公司', '有限责任公司', '有限公司',
    '公司', '集团', '大学', '学院', '研究院', '研究所',
    '中心', '医院', '银行', '协会', '基金会', '机构', '组织',
]

# API配置
API_HOST = '0.0.0.0'
API_PORT = 5000
