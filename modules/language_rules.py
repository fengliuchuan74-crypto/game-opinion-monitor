"""Small, explainable lexical baseline. Uncertain language stays in review."""
import re
import unicodedata

VARIANTS = str.maketrans(dict(zip(
    '閃潰頓網絡線斷連伺穩戲遊愛歡厭爛騙錢費廣誤帳號隱個資獎勵譯體驗優還進開無價貴務隊暢薦錯滿夠嗎這個後動態',
    '闪溃顿网络线断连伺稳戏游爱欢厌烂骗钱费广误账号隐个资奖励译体验优还进开无价贵务队畅荐错满够吗这个后动态')))
VARIANTS.update(str.maketrans({'沒':'没','斷':'断','請':'请','問':'问','題':'题','單':'单','遲':'迟','誇':'夸'}))


def normalize(text):
    return unicodedata.normalize('NFKC', str(text)).lower().translate(VARIANTS)


def occurrences(text, term):
    expression = re.escape(term)
    if re.search('[a-z]', term):
        expression = r'(?<![a-z])' + expression + r'(?![a-z])'
    return list(re.finditer(expression, text))


def negated(text, start):
    prefix = re.split(r'[，。！？,.;!？\n]|\bbut\b|但是|不过', text[:start])[-1]
    if re.search(r'(?:不|没|没有|并非|不会|不再|并不|无)(?:再|会|有|很|太|怎么|什么|出现|发生|任何|一直|那么){0,3}$', prefix):
        return True
    return bool(re.search(r"\b(?:not|no|never|without|isn't|isnt|don't|doesn't|doesnt|no longer)\s+(?:\w+\s+){0,2}$", prefix))


def active_hits(text, terms):
    text = normalize(text)
    return [term for term in terms if any(not negated(text, match.start())
            for match in occurrences(text, normalize(term)))]
