from __future__ import annotations

import re


def _counts(text):
    value = str(text or '')
    han = len(re.findall(r'[\u3400-\u9fff]', value))
    latin = len(re.findall(r'[A-Za-z]', value))
    return han, latin


def chinese_bibliography(title, abstract=''):
    han, latin = _counts(title)
    if han >= 2 and han / max(1, han + latin) >= .4:
        return True
    han, latin = _counts(abstract)
    return han >= 60 and han / max(1, han + latin) >= .4


def chinese_page(text):
    han, latin = _counts(text)
    return han >= 40 and han / max(1, han + latin) >= .35
