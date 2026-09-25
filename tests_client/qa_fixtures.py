"""Explicit demonstration records, isolated from the user's library."""
import json
from pathlib import Path
from test_backend import fixture_pdf

root = Path(__file__).resolve().parents[1] / 'docs/verification/runtime'
root.mkdir(parents=True, exist_ok=True)
fixture_pdf(root / 'verification.pdf')
entries = [
    ('Attention Is All You Need', 'Vaswani', 2017, 'NeurIPS', '10.48550/arXiv.1706.03762'),
    ('Deep Residual Learning for Image Recognition', 'He', 2016, 'CVPR', '10.1109/CVPR.2016.90'),
    ('BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding', 'Devlin', 2019, 'NAACL', '10.18653/v1/N19-1423'),
    ('Language Models are Few-Shot Learners', 'Brown', 2020, 'NeurIPS', '10.48550/arXiv.2005.14165'),
    ('Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks', 'Lewis', 2020, 'NeurIPS', '10.48550/arXiv.2005.11401'),
    ('Deep learning', 'LeCun', 2015, 'Nature', '10.1038/nature14539'),
    ('Batch Normalization: Accelerating Deep Network Training by Reducing Internal Covariate Shift', 'Ioffe', 2015, 'ICML', '10.48550/arXiv.1502.03167'),
    ('U-Net: Convolutional Networks for Biomedical Image Segmentation', 'Ronneberger', 2015, 'MICCAI', '10.1007/978-3-319-24574-4_28'),
]
records = [{'title': t, 'author': [{'family': a}], 'issued': {'date-parts': [[y]]}, 'container-title': c, 'DOI': d, 'type': 'paper-conference' if c != 'Nature' else 'article-journal',
            'tags': ['基础文献', '界面验收示例'], 'readingState': ['reading', 'read', 'unread'][n % 3],
            'abstract': '界面验收示例题录，用于检查列表、阅读与笔记流程。测试附件由程序生成，并非此论文原文。' if n == 0 else '',
            'ISSN': '0028-0836' if c == 'Nature' else ''} for n, (t, a, y, c, d) in enumerate(entries)]
(root / 'demo-csl.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
print('QA fixtures created; no personal library modified.')
