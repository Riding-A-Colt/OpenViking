"""Local BM25 sidecar index for OpenViking memories (SQLite FTS5 + trigram).

设计来自 ``~/workflow/ov-hybrid/PLAN.md`` §4：
只读一份记忆文件快照建一张 FTS5 表，用 trigram 分词，查询侧做 BM25 排序，
再手工截字符窗口做片段（FTS5 的 ``snippet()`` 在 trigram 下会错位，禁用）。

对外契约（第 2 段按这个调）：

    engine = LocalFTSEngine(db_path)
    engine.refresh_if_stale()      # {"rebuilt": bool, "docs": int, "ms": float}
    engine.search("commit_token_threshold", limit=8)
    engine.stats()                 # {"docs": int, "db_bytes": int, "last_build_ms": float}

模块级工具：``normalize`` / ``extract_terms`` / ``make_snippet``。

硬约定：
* 只用标准库；
* import 时不产生任何 I/O（不建库、不扫目录、不连 sqlite），一切懒执行；
* ``search()`` 永不抛异常，出错记 debug 日志并返回 ``[]``；
* 重建走「临时文件 + ``os.replace``」并在重建前拿 ``fcntl.flock`` 排它锁，
  拿不到锁就跳过、沿用旧库。
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

LOG = logging.getLogger(__name__)

#: 语料根：每个 root 下面按 ``memories/**`` 与 ``peers/*/memories/**`` 取文件。
DEFAULT_ROOTS = ("~/.openviking/data/viking/default/user/default",)

#: 自动生成、内容只是链接表的索引文件，不进索引。
EXCLUDE_NAMES = frozenset({".overview.md", ".abstract.md"})

#: 单篇正文超过这个长度就要切片（PLAN §5.3）。
SHORT_DOC_CHARS = 800

#: 命中点落在正文首 N 字内时，直接给开头窗口（PLAN §5.3 第 4 条）。
HEAD_WINDOW_CHARS = 400

#: 中文词条滑窗上界（PLAN §4.4）。
MAX_CJK_TERM = 6
MIN_CJK_TERM = 2

#: trigram 索引对 <3 字符的串无效，这些词条只能走 LIKE。
TRIGRAM_MIN_CHARS = 3

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\\-]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

#: 英文停用词（只用于查询词条，不影响索引内容）。
_ASCII_STOPWORDS = frozenset(
    """a an the and or not but for with without from into onto of to in on at by as is are was were be been
    being this that these those it its he she they them we you your my me our do does did done have has had
    can could should would will may might must if then than there here what which who whom how why when where
    about over under again more most some any all each every no nor so such too very just only own same""".split()
)

#: 中文停用字：出现在词条首/尾即视为噪声词条。
_CJK_STOP_CHARS = set(
    "的了着过是在有我你他她它和与及或就都也不没这那有什怎吗呢吧啊哦呀嘛"
    "个把被给让对从向为以之而且所其之为于则即却并再还又很太更最些该此"
)

#: 中文停用短语：整词命中即丢。
_CJK_STOP_PHRASES = frozenset(
    """什么 怎么 怎样 如何 为什 么样 这个 那个 这些 那些 一个 一下 我们 你们 他们 可以 需要 现在
    然后 但是 因为 所以 如果 就是 还是 已经 没有 不是 这样 那样 时候 东西 问题 情况 请问 帮我
    的是 了的 了吗 了吗 是的 好的""".split()
)


def _fold(text: str) -> str:
    """NFKC + casefold，尽量保持字符数不变（片段定位用）。"""
    if not text:
        return ""
    try:
        return unicodedata.normalize("NFKC", str(text)).casefold()
    except Exception:  # pragma: no cover - unicodedata 不会失败，兜底而已
        return str(text).casefold()


def normalize(text: str) -> str:
    """全角→半角、大小写折叠、空白折叠。查询与索引正文共用。"""
    if not text:
        return ""
    return re.sub(r"\s+", " ", _fold(text)).strip()


def _is_noise_ascii(token: str) -> bool:
    """过滤 ``---`` / ``...`` / ``/`` 这类纯符号串与停用词。"""
    if len(token) < 3:
        return True
    if not re.search(r"[A-Za-z0-9]", token):
        return True
    if token in _ASCII_STOPWORDS:
        return True
    # 全是同一字符的重复（----、....）
    if len(set(token)) == 1:
        return True
    return False


def _cjk_grams(run: str, limit_chars: int = 120) -> list[tuple[int, int, str]]:
    """对一段连续中文取 2–6 字滑窗，返回 (起始位置, 长度, 词条)。"""
    run = run[:limit_chars]
    out: list[tuple[int, int, str]] = []
    n = len(run)
    for size in range(min(MAX_CJK_TERM, n), MIN_CJK_TERM - 1, -1):
        for i in range(0, n - size + 1):
            out.append((i, size, run[i : i + size]))
    return out


def extract_terms(query: str, max_terms: int = 12) -> list[str]:
    """从查询里抽字面词条：ASCII 标识符 + 中文 2–6 字滑窗。

    顺序：标识符在前（按出现顺序），中文长词在前（同长按出现顺序）。
    ASCII 词条去停用词；中文词条丢掉以停用字开头/结尾的，以及整词是停用短语的。
    """
    if not query:
        return []
    text = unicodedata.normalize("NFKC", str(query))
    seen: set[str] = set()
    terms: list[str] = []

    def push(term: str) -> None:
        key = term.casefold()
        if key in seen:
            return
        seen.add(key)
        terms.append(term)

    # 1) ASCII 标识符（常量名、路径、文件名、版本号）
    for m in _ASCII_TOKEN_RE.finditer(text):
        tok = m.group(0).strip("._-/\\")
        if not tok or _is_noise_ascii(tok):
            continue
        push(tok)
        if len(terms) >= max_terms:
            return terms[:max_terms]

    # 2) 中文滑窗
    grams: list[tuple[int, int, int, str]] = []
    for m in _CJK_RUN_RE.finditer(text):
        for pos, size, gram in _cjk_grams(m.group(0)):
            if gram in _CJK_STOP_PHRASES:
                continue
            if gram[0] in _CJK_STOP_CHARS or gram[-1] in _CJK_STOP_CHARS:
                continue
            if all(ch in _CJK_STOP_CHARS for ch in gram):
                continue
            grams.append((-size, pos, size, gram))

    grams.sort(key=lambda t: (t[0], t[1]))
    for _neg, _size, _pos, gram in grams:
        push(gram)
        if len(terms) >= max_terms:
            break
    return terms[:max_terms]
def _strip_noise(raw: str) -> str:
    """去掉 YAML frontmatter 与 HTML 注释（``<!-- MEMORY_FIELDS ... -->``）。"""
    text = raw.lstrip("\ufeff")
    if text.startswith("---"):
        m = re.match(r"^---\s*\n.*?\n---\s*\n?", text, re.S)
        if m:
            text = text[m.end() :]
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return text.strip()


def _extract_title(body: str, path: str) -> str:
    """首个 h1；没有就用首个非空行；都没有就退回文件名主干。"""
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            title = s.lstrip("#").strip()
            if title:
                return title[:120]
        return s[:120]
    return Path(path).stem[:120]


def _match_pos(body: str, term: str) -> int:
    """在正文里定位词条，返回字符下标（找不到 -1）。"""
    if not body or not term:
        return -1
    pos = body.find(term)
    if pos >= 0:
        return pos
    folded = _fold(body)
    target = _fold(term)
    if not target:
        return -1
    pos = folded.find(target)
    if pos < 0:
        return -1
    # NFKC 极少数情况会改变字符数，越界时退回原串搜索的结果
    return pos if pos < len(body) else -1


def make_snippet(
    body: str,
    terms: list[str] | None = None,
    nchars: int = 200,
    short_doc_chars: int = SHORT_DOC_CHARS,
) -> str:
    """按 PLAN §5.3 生成片段。

    * 正文 < ``short_doc_chars``：整篇返回；
    * 否则用 ``str.find`` 定位首个命中词条，截 ``[pos-nchars, pos+len(term)+nchars]``，两端补省略号；
    * 命中点落在首 ``HEAD_WINDOW_CHARS`` 字内：给开头窗口（≤``short_doc_chars``，等同短文件路径）；
    * 一个词条都没找到：给开头窗口。

    不使用 FTS5 的 ``snippet()``：trigram 分词下 token 是三字窗口，切出来会错位。
    """
    if not body:
        return ""
    text = body.strip()
    if len(text) <= short_doc_chars:
        return text

    pos, hit_len = -1, 0
    for term in terms or []:
        pos = _match_pos(text, term)
        if pos >= 0:
            hit_len = len(term)
            break

    if pos < 0 or pos < HEAD_WINDOW_CHARS:
        head = text[:short_doc_chars]
        return head + ("…" if len(text) > short_doc_chars else "")

    start = max(0, pos - nchars)
    end = min(len(text), pos + hit_len + nchars)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _iter_markdown(roots: tuple[str, ...], corpus_root: str | None = None):
    """按 PLAN §4.2 的覆盖范围产出 ``(abs_path, rel_path)``。

    只取 ``<root>/memories/**/*.md`` 与 ``<root>/peers/*/memories/**/*.md``，
    天然排除 ``sessions/**``；再排除 ``.overview.md`` / ``.abstract.md``。
    """
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        try:
            pats = (base.glob("memories/**/*.md"), base.glob("peers/*/memories/**/*.md"))
            for it in pats:
                for path in it:
                    name = path.name
                    if name in EXCLUDE_NAMES or name.startswith("."):
                        continue
                    if not path.is_file():
                        continue
                    try:
                        rel = path.relative_to(base).as_posix()
                    except ValueError:  # pragma: no cover
                        rel = path.name
                    yield path, rel
        except OSError as exc:  # pragma: no cover - 权限问题
            LOG.debug("local_fts: scan failed under %s: %s", base, exc)
class LocalFTSEngine:
    """记忆文件 → SQLite FTS5(trigram) 索引 + BM25 查询 + 片段提取。

    参数：
        db_path: 索引库路径（目录不存在会自动创建）。
        snippet_chars: 长文档片段在命中点前后各取多少字符。
        short_doc_chars: 短于等于该长度的正文整篇返回。
        max_terms: 单次查询最多用多少个词条。
        roots: 语料根目录列表（默认 ``~/.openviking/data/viking/default/user/default``）。
        uri_prefix: 生成 viking URI 的前缀。
    """

    def __init__(
        self,
        db_path: str,
        *,
        snippet_chars: int = 200,
        short_doc_chars: int = SHORT_DOC_CHARS,
        max_terms: int = 12,
        roots=None,
        uri_prefix: str = "viking://user/",
    ) -> None:
        self.db_path = os.path.abspath(os.path.expanduser(str(db_path)))
        self.snippet_chars = int(snippet_chars)
        self.short_doc_chars = int(short_doc_chars)
        self.max_terms = int(max_terms)
        self.roots = tuple(roots) if roots else DEFAULT_ROOTS
        self.uri_prefix = uri_prefix
        self._conn: sqlite3.Connection | None = None
        self._conn_sig: tuple | None = None
        self._last_build_ms: float = 0.0
        self._last_docs: int = 0

    # ---------------------------------------------------------------- 内部

    @property
    def lock_path(self) -> str:
        return self.db_path + ".lock"

    def _db_sig(self) -> tuple | None:
        try:
            st = os.stat(self.db_path)
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._conn_sig = None

    def _connect(self) -> sqlite3.Connection | None:
        """只读连接，按文件 inode/mtime/size 缓存与失效。"""
        sig = self._db_sig()
        if sig is None:
            self._close()
            return None
        if self._conn is not None and self._conn_sig == sig:
            return self._conn
        self._close()
        try:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            LOG.debug("local_fts: cannot open %s: %s", self.db_path, exc)
            return None
        self._conn, self._conn_sig = conn, sig
        return conn

    def _read_meta(self) -> dict:
        """读库里的 meta 表；库不存在/损坏时返回 {}。"""
        conn = self._connect()
        if conn is None:
            return {}
        try:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            return {r["key"]: r["value"] for r in rows}
        except sqlite3.Error as exc:
            LOG.debug("local_fts: meta read failed: %s", exc)
            return {}

    def _scan(self) -> tuple[list[tuple[str, str, int, int]], str]:
        """扫描语料，返回 ([(rel, abs, mtime_ns, size)], fingerprint)。"""
        items: list[tuple[str, str, int, int]] = []
        total = 0
        max_mtime = 0
        seen_abs: set[str] = set()
        for abs_path, rel in _iter_markdown(self.roots):
            if abs_path in seen_abs:  # 同一文件被多个 root 覆盖时只算一次
                continue
            seen_abs.add(abs_path)
            try:
                st = os.stat(abs_path)
            except OSError:
                continue
            items.append((rel, str(abs_path), st.st_mtime_ns, st.st_size))
            total += st.st_size
            if st.st_mtime_ns > max_mtime:
                max_mtime = st.st_mtime_ns
        items.sort(key=lambda t: t[0])
        fp = f"n{len(items)}:m{max_mtime}:b{total}"
        return items, fp

    def _uri_for(self, rel: str, abs_path: str) -> str:
        """``<root>/memories/x.md`` → ``viking://user/<root名>/memories/x.md``。"""
        try:
            base = Path(abs_path)
            for root in self.roots:
                rp = Path(root).expanduser()
                try:
                    abs_path_rel = base.relative_to(rp)
                except ValueError:
                    continue
                return f"{self.uri_prefix}{rp.name}/{abs_path_rel.as_posix()}"
        except Exception:  # pragma: no cover
            pass
        return f"{self.uri_prefix}{rel}"

    # ---------------------------------------------------------------- 建索引

    def _build(self, items: list[tuple[str, str, int, int]], fp: str) -> tuple[int, float]:
        """在临时文件里全量重建，成功后 ``os.replace`` 原子替换。"""
        t0 = time.perf_counter()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        tmp = f"{self.db_path}.tmp.{os.getpid()}"
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

        conn = sqlite3.connect(tmp, timeout=10.0)
        try:
            # tmp 是一次性文件，只有 os.replace 之后才可见：
            # 关掉 journal 省下 ~20% 重建时间与 ~3 MB 体积（实测，见 logs/stage1-selftest.txt）
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA cache_size=-16384")
            conn.execute(
                """
                CREATE TABLE docs(
                  path TEXT PRIMARY KEY,
                  uri  TEXT NOT NULL,
                  title TEXT,
                  mtime REAL,
                  nchars INTEGER,
                  body TEXT,
                  norm TEXT
                )
                """
            )
            conn.execute(
                "CREATE VIRTUAL TABLE docs_fts USING fts5(path UNINDEXED, body, tokenize='trigram')"
            )
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")

            docs_rows = []
            fts_rows = []
            seen_rel: set[str] = set()
            for rel, abs_path, mtime_ns, _size in items:
                if rel in seen_rel:  # 多 root 下同相对路径：先到先得
                    continue
                try:
                    raw = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    LOG.debug("local_fts: read failed %s: %s", abs_path, exc)
                    continue
                body = _strip_noise(raw)
                if not body:
                    continue
                seen_rel.add(rel)
                norm = normalize(body)
                rid = len(docs_rows) + 1
                docs_rows.append(
                    (rid, rel, self._uri_for(rel, abs_path), _extract_title(body, rel),
                     mtime_ns / 1e9, len(body), body, norm)
                )
                fts_rows.append((rid, rel, norm))

            conn.executemany(
                "INSERT INTO docs(rowid, path, uri, title, mtime, nchars, body, norm) "
                "VALUES(?,?,?,?,?,?,?,?)",
                docs_rows,
            )
            conn.executemany("INSERT INTO docs_fts(rowid, path, body) VALUES(?,?,?)", fts_rows)
            conn.commit()

            ms = (time.perf_counter() - t0) * 1000.0
            n = len(docs_rows)
            meta = {
                "fp": fp,
                "docs": str(n),
                "last_build_ms": f"{ms:.2f}",
                "built_at": f"{time.time():.3f}",
            }
            conn.executemany("INSERT INTO meta(key, value) VALUES(?,?)", list(meta.items()))
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # 落盘后替换；rename 同目录原子
        try:
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
        os.replace(tmp, self.db_path)
        ms = (time.perf_counter() - t0) * 1000.0
        self._last_build_ms = ms
        self._last_docs = len(docs_rows)
        self._close()
        LOG.debug("local_fts: rebuilt %d docs in %.1f ms", len(docs_rows), ms)
        return len(docs_rows), ms

    def refresh_if_stale(self) -> dict:
        """比对 (文件数, 最大 mtime, 总字节)；不一致则加锁重建。

        永远不抛异常。拿不到锁就跳过并沿用旧库。
        """
        t0 = time.perf_counter()
        try:
            items, fp = self._scan()
        except Exception as exc:
            LOG.debug("local_fts: scan failed: %s", exc)
            return {"rebuilt": False, "docs": self._meta_docs(), "ms": 0.0}

        meta = self._read_meta()
        if meta.get("fp") == fp and self._db_sig() is not None:
            return {"rebuilt": False, "docs": int(meta.get("docs", len(items)) or 0),
                    "ms": round((time.perf_counter() - t0) * 1000.0, 2)}

        lock_fd = None
        try:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # 别的进程正在重建：跳过，用旧库
            LOG.debug("local_fts: lock busy (%s), keep old index", exc)
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
            return {"rebuilt": False, "docs": self._meta_docs(), "ms": 0.0, "skipped": "locked"}

        try:
            n, build_ms = self._build(items, fp)
            return {"rebuilt": True, "docs": n, "ms": round(build_ms, 2)}
        except Exception as exc:
            LOG.debug("local_fts: rebuild failed: %s", exc)
            return {"rebuilt": False, "docs": self._meta_docs(),
                    "ms": round((time.perf_counter() - t0) * 1000.0, 2), "skipped": "error"}
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass

    def _meta_docs(self) -> int:
        try:
            return int(self._read_meta().get("docs", 0) or 0)
        except Exception:
            return 0
    # ---------------------------------------------------------------- 查询

    @staticmethod
    def _match_expr(terms: list[str]) -> str:
        """每个词条双引号包裹、内部 ``"`` 转义成 ``""``，整体 OR 连接。

        FTS5 的 MATCH 语法对 ``voice-chain.test.ts`` 这类带符号的串很敏感，
        不加引号会抛 syntax error；引号包裹后按短语匹配（trigram 下即连续三字窗口）。
        """
        parts = []
        for term in terms:
            if not term:
                continue
            parts.append('"' + term.replace('"', '""') + '"')
        return " OR ".join(parts)

    def _query_match(
        self, conn: sqlite3.Connection, terms: list[str], limit: int
    ) -> list[tuple[sqlite3.Row, float]]:
        expr = self._match_expr(terms)
        if not expr:
            return []
        sql = (
            "SELECT d.path AS path, d.uri AS uri, d.title AS title, d.nchars AS nchars, "
            "d.body AS body, bm25(docs_fts) AS rank "
            "FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
            "WHERE docs_fts MATCH ? ORDER BY rank ASC LIMIT ?"
        )
        rows = conn.execute(sql, (expr, int(limit))).fetchall()
        # bm25() 越负越相关，取负后越大越相关
        return [(r, float(-r["rank"])) for r in rows]

    def _query_like(
        self, conn: sqlite3.Connection, term: str, limit: int
    ) -> list[tuple[sqlite3.Row, float]]:
        """1–2 字查询与 MATCH 空结果兜底：全表 LIKE，按命中次数排序。"""
        needle = normalize(term)
        if not needle:
            return []
        esc = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        try:
            rows = conn.execute(
                "SELECT path, uri, title, nchars, body, norm FROM docs WHERE norm LIKE ? ESCAPE '\\'",
                (f"%{esc}%",),
            ).fetchall()
        except sqlite3.Error as exc:
            LOG.debug("local_fts: LIKE query failed: %s", exc)
            return []
        scored = []
        for r in rows:
            hits = (r["norm"] or "").count(needle)
            scored.append((hits, -int(r["nchars"] or 0), r))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [(r, float(hits)) for hits, _n, r in scored[:limit]]

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """BM25 查询，返回按相关度降序的片段列表。**任何异常都不抛出**。"""
        try:
            text = (query or "").strip()
            limit = max(0, int(limit))
            if not text or limit == 0:
                return []
            terms = extract_terms(text, self.max_terms)
            if not terms:
                return []
            if self._db_sig() is None:
                # 首次使用：库还没有，建一次（之后由调用方显式 refresh_if_stale）
                self.refresh_if_stale()
            conn = self._connect()
            if conn is None:
                return []

            long_terms = [t for t in terms if len(t) >= TRIGRAM_MIN_CHARS]
            rows: list[tuple[sqlite3.Row, float]] = []
            if long_terms:
                try:
                    rows = self._query_match(conn, long_terms, limit)
                except sqlite3.Error as exc:
                    LOG.debug("local_fts: MATCH failed for %r: %s", long_terms, exc)
                    rows = []
            if not rows:
                # 2 字查询，或 MATCH 因词条过短/罕见而无结果：回退 LIKE
                for cand in sorted(terms, key=len, reverse=True)[:3]:
                    rows = self._query_like(conn, cand, limit)
                    if rows:
                        break

            ordered = sorted(terms, key=len, reverse=True)
            out: list[dict] = []
            for r, score in rows:
                body = r["body"] or ""
                out.append(
                    {
                        "path": r["path"],
                        "uri": r["uri"],
                        "title": r["title"] or "",
                        "score": float(score),
                        "snippet": make_snippet(
                            body, ordered, self.snippet_chars, self.short_doc_chars
                        ),
                        "nchars": int(r["nchars"] or len(body)),
                    }
                )
            return out
        except Exception as exc:  # 契约：永不抛出
            LOG.debug("local_fts: search failed for %r: %s", query, exc, exc_info=True)
            return []

    # ---------------------------------------------------------------- 统计

    def stats(self) -> dict:
        """{"docs": int, "db_bytes": int, "last_build_ms": float}；失败返回零值。"""
        try:
            meta = self._read_meta()
            try:
                db_bytes = os.path.getsize(self.db_path)
            except OSError:
                db_bytes = 0
            docs = int(meta.get("docs", 0) or 0) or self._last_docs
            last_ms = float(meta.get("last_build_ms", 0) or 0) or self._last_build_ms
            return {"docs": docs, "db_bytes": db_bytes, "last_build_ms": round(last_ms, 2)}
        except Exception as exc:
            LOG.debug("local_fts: stats failed: %s", exc)
            return {"docs": 0, "db_bytes": 0, "last_build_ms": 0.0}
