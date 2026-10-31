"""EPUB container access: spine order, manifest, stylesheets and resources."""
from __future__ import annotations

import posixpath
import zipfile

from lxml import etree

OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
DC_NS = "http://purl.org/dc/elements/1.1/"
XHTML_NS = "http://www.w3.org/1999/xhtml"

XHTML_MEDIA = {"application/xhtml+xml", "text/html"}


class Epub:
    def __init__(self, path: str):
        self.path = path
        self.zip = zipfile.ZipFile(path)
        self._names = {n.lstrip("./"): n for n in self.zip.namelist()}
        self.opf_path = self._find_opf()
        self.opf_dir = posixpath.dirname(self.opf_path)
        opf = etree.fromstring(self.zip.read(self.opf_path))
        self.metadata = self._read_metadata(opf)
        self.manifest = self._read_manifest(opf)
        self.spine = self._read_spine(opf)

    # ---------------------------------------------------------------- reading

    def _find_opf(self) -> str:
        try:
            container = etree.fromstring(self.zip.read("META-INF/container.xml"))
            node = container.find(".//{%s}rootfile" % CONTAINER_NS)
            if node is not None and node.get("full-path"):
                return node.get("full-path")
        except KeyError:
            pass
        for name in self.zip.namelist():
            if name.endswith(".opf"):
                return name
        raise ValueError("no OPF package document found in %s" % self.path)

    def _read_metadata(self, opf) -> dict:
        meta = {}
        for child in opf.iter("{%s}*" % DC_NS):
            tag = etree.QName(child).localname
            if child.text and tag not in meta:
                meta[tag] = child.text.strip()
        return meta

    def _read_manifest(self, opf) -> dict:
        items = {}
        for item in opf.iter("{%s}item" % OPF_NS):
            href = item.get("href")
            if not href:
                continue
            items[item.get("id")] = {
                "href": href,
                "path": self.resolve(self.opf_dir, href),
                "media_type": item.get("media-type", ""),
                "properties": item.get("properties", ""),
            }
        return items

    def _read_spine(self, opf) -> list[dict]:
        docs = []
        for ref in opf.iter("{%s}itemref" % OPF_NS):
            item = self.manifest.get(ref.get("idref"))
            if not item:
                continue
            if item["media_type"] in XHTML_MEDIA or item["path"].endswith((".xhtml", ".html", ".htm")):
                docs.append(item)
        if not docs:
            docs = [i for i in self.manifest.values() if i["media_type"] in XHTML_MEDIA]
        return docs

    # ------------------------------------------------------------- resources

    @staticmethod
    def resolve(base_dir: str, href: str) -> str:
        href = href.split("#", 1)[0]
        joined = posixpath.join(base_dir, href) if base_dir else href
        return posixpath.normpath(joined).lstrip("./")

    def read(self, path: str) -> bytes:
        name = self._names.get(path.lstrip("./"))
        if name is None:
            raise KeyError(path)
        return self.zip.read(name)

    def exists(self, path: str) -> bool:
        return path.lstrip("./") in self._names

    def read_text(self, path: str) -> str:
        raw = self.read(path)
        for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    def parse_xhtml(self, path: str):
        raw = self.read(path)
        parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
        try:
            root = etree.fromstring(raw, parser)
            if root is not None:
                return root
        except etree.XMLSyntaxError:
            pass
        return etree.fromstring(raw, etree.HTMLParser(recover=True, huge_tree=True))

    def stylesheets_for(self, doc_path: str, root) -> list[str]:
        """Return CSS text for a document: linked stylesheets then inline <style>."""
        base = posixpath.dirname(doc_path)
        sheets = []
        for link in root.iter():
            tag = _local(link.tag)
            if tag == "link":
                rel = (link.get("rel") or "").lower()
                href = link.get("href")
                if href and ("stylesheet" in rel or (link.get("type") or "").endswith("css")):
                    target = self.resolve(base, href)
                    if self.exists(target):
                        sheets.append(self.read_text(target))
            elif tag == "style" and link.text:
                sheets.append(link.text)
        return sheets

    @property
    def title(self) -> str:
        return self.metadata.get("title", "")


def _local(tag) -> str | None:
    if not isinstance(tag, str):
        return None
    return tag.rsplit("}", 1)[-1].lower()
