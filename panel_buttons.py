"""Localize only JOHAALETRADER's public panel entry; preserve unrelated buttons."""
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
from telegram import InlineKeyboardButton,InlineKeyboardMarkup

def panel_url(url):
    if not isinstance(url,str):return None
    try:
        parts=urlsplit(url)
        if parts.scheme!='https' or parts.netloc.lower()!='t.me' or parts.path.strip('/').lower()!='johaaletrader_bot':return None
        args=parse_qsl(parts.query,keep_blank_values=True)
        starts=[v for k,v in args if k=='start']
        if len(starts)!=1 or starts[0].lower() not in {'panel','panel_en'}:return None
        return urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode([(k,'panel_en' if k=='start' else v) for k,v in args]),parts.fragment))
    except (ValueError,TypeError):return None

def has_panel_button(markup):
    return bool(markup and any(panel_url(getattr(b,'url',None)) for row in markup.inline_keyboard for b in row))

def localize_panel_markup(markup,*,english):
    if not english or not has_panel_button(markup):return markup
    rows=[]
    for row in markup.inline_keyboard:
        new=[]
        for button in row:
            url=panel_url(getattr(button,'url',None))
            if not url:new.append(button);continue
            payload=button.to_dict();payload.update(text='🖥 Discover the panel',url=url)
            new.append(InlineKeyboardButton.de_json(payload,bot=None))
        rows.append(new)
    return InlineKeyboardMarkup(rows)
