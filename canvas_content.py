"""Small, shared rich-text conversion and Canvas asset discovery."""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


def web_url(value, base=""):
    value = urljoin(base, value or "")
    return value if urlparse(value).scheme in {"https", "http"} else None


def file_id(value, base):
    target, origin = urlparse(urljoin(base, value)), urlparse(base)
    if target.netloc.lower() != origin.netloc.lower():
        return None
    match = re.search(r"/files/(\d+)(?:/|$)", target.path)
    return match.group(1) if match else None


def rich_text(value, base=""):
    """Keep destinations, image alt text, table rows and equation alternatives."""
    soup = BeautifulSoup(value or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all("a", href=True):
        target = web_url(tag["href"], base)
        if target:
            label = tag.get_text(" ", strip=True)
            tag.replace_with(f"{label} — {target}" if label and label != target else target)
    for tag in soup.find_all(["img", "iframe", "video", "audio", "source", "track", "object", "embed"]):
        target = web_url(tag.get("src") or tag.get("data"), base)
        label = tag.get("alt") or tag.get("title") or tag.get("data-equation-content") or tag.name
        tag.replace_with(f"[{label}]" + (f" {target}" if target else ""))
    for row in soup.find_all("tr"):
        row.replace_with(" | ".join(cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)) + "\n")
    return soup.get_text("\n", strip=True)


def references(value, base):
    """Return unique Canvas file URLs and other web references without crawling."""
    soup = BeautifulSoup(value or "", "html.parser")
    assets, links = {}, {}
    for tag in soup.find_all(True):
        for attr in ("href", "src", "data", "poster"):
            if not tag.get(attr):
                continue
            target = web_url(tag[attr], base)
            if not target:
                continue
            identifier = file_id(target, base)
            if identifier:
                assets.setdefault(identifier, target)
            else:
                links.setdefault(target, None)
    return list(assets.values()), list(links)


def fidelity_html(value):
    """Retain rich layouts only when plain text cannot represent the content."""
    soup = BeautifulSoup(value or "", "html.parser")
    return bool(soup.find(["img", "table", "math", "svg", "iframe", "video", "audio", "object"]))


def offline_html(value, base, local_files):
    """An inert HTML companion with local file references where available."""
    soup = BeautifulSoup(value or "", "html.parser")
    for tag in soup(["script", "style", "base", "form", "meta", "link"]):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on") or attr in {"srcdoc", "action"}:
                del tag[attr]
        for attr in ("href", "src", "data", "poster"):
            if attr not in tag.attrs:
                continue
            target = web_url(tag[attr], base)
            identifier = file_id(target or "", base)
            if identifier in local_files:
                tag[attr] = local_files[identifier]
            elif target:
                tag[attr] = target
            elif not str(tag[attr]).startswith("#"):
                del tag[attr]
    return ('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" '
            'content="default-src \'none\'; img-src \'self\' data:; media-src \'self\'; '
            'style-src \'unsafe-inline\'; frame-src \'none\'">' + str(soup))
