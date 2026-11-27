#!/usr/bin/env python3
"""Lista editable de hosts del panel de administración."""
from __future__ import annotations
import ipaddress, json, os, re, tempfile, threading
from urllib.parse import urlsplit
MAX_DOMAINS=500
MAX_DOMAIN_LENGTH=253
_CONFIG=os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "wayviewer")
_STORE=os.path.join(_CONFIG,"blocked-hosts.json")
_LABEL=re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_LOCK=threading.Lock()

def normalize(value: object)->str|None:
    if not isinstance(value,str): return None
    text=value.strip()
    if not text:return None
    try:
        host=urlsplit(text).hostname if "://" in text else urlsplit("//"+text).hostname
    except ValueError:return None
    host=(host or "").strip().strip(".").lower()
    if host.startswith("*."):host=host[2:]
    try:host=host.encode("idna").decode("ascii").lower()
    except UnicodeError:return None
    if not host or len(host)>MAX_DOMAIN_LENGTH:return None
    try:ipaddress.ip_address(host);return None
    except ValueError:pass
    labels=host.split(".")
    if len(labels)<2 or not all(_LABEL.match(x) for x in labels):return None
    return host

def validate_list(items:object)->tuple[list[str]|None,str]:
    if not isinstance(items,list):return None,"la lista no es válida"
    if len(items)>MAX_DOMAINS:return None,f"máximo {MAX_DOMAINS} dominios"
    valid=[];invalid=[]
    for item in items:
        domain=normalize(item)
        if domain is None:invalid.append(str(item)[:80])
        elif domain not in valid:valid.append(domain)
    if invalid:return None,"dominio inválido: "+", ".join(invalid[:3])
    # Mantener el orden de entrada para emitir /etc/hosts igual que la UI.
    return valid,""

def load_local()->list[str]:
    try:
        with open(_STORE,encoding="utf-8") as f:data=json.load(f)
    except (OSError,ValueError):return []
    valid,error=validate_list(data)
    return valid if not error else []

def save_local(domains:list[str])->None:
    os.makedirs(_CONFIG,exist_ok=True)
    payload=json.dumps(domains,ensure_ascii=True)+"\n"
    with _LOCK:
        h=tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=_CONFIG,prefix=".hosts-",delete=False)
        tmp=h.name
        try:
            with h:h.write(payload);h.flush();os.fsync(h.fileno())
            os.replace(tmp,_STORE)
        except OSError:
            try:os.unlink(tmp)
            except OSError:pass
            raise
