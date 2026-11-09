import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

const STATUS = `query { status { state filesSeen filesIndexed filesSkipped filesFailed lastError priorityFolder libraryTotal progress currentFile } }`;

const SEARCH = `query ($q: String!, $fileType: FileType!, $folder: String, $limit: Int!) {
  search(q: $q, fileType: $fileType, folder: $folder, limit: $limit) {
    tookMs
    hits { path name fileType page snippet score similarity }
  }
}`;

const PRIORITIZE = `mutation ($path: String!) { prioritizeFolder(path: $path) }`;
const STOP_INDEXING = `mutation { stopIndexing }`;
const RESUME_INDEXING = `mutation { resumeIndexing }`;

const FILTERS = [
  { id: "ALL", label: "All" },
  { id: "DOCUMENT", label: "Documents" },
  { id: "IMAGE", label: "Photos" },
];

async function gql(query, variables) {
  const response = await fetch("/graphql", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  const body = await response.json();
  if (body.errors?.length) {
    throw new Error(body.errors[0].message);
  }
  return body.data;
}

function queryForms(query) {
  const tokens = query.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];
  const forms = [];
  for (const token of tokens) {
    forms.push(token);
    if (token.length >= 5 && token.endsWith("s")) forms.push(token.slice(0, -1));
  }
  return [...new Set(forms)];
}

function highlight(text, query) {
  const tokens = queryForms(query);
  if (!tokens.length || !text) return text;
  const pattern = new RegExp(`(${tokens.map(escapeRegExp).join("|")})`, "ig");
  const parts = text.split(pattern);
  return parts.map((part, index) =>
    tokens.includes(part.toLowerCase()) ? <mark key={index}>{part}</mark> : <span key={index}>{part}</span>,
  );
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function progressLabel(status) {
  const seen = status.filesSeen.toLocaleString();
  if (status.libraryTotal > 0 && typeof status.progress === "number") {
    return `${seen} / ${status.libraryTotal.toLocaleString()} · ${status.progress}%`;
  }
  return `${seen} files`;
}

function statusParts(status) {
  if (!status) return { label: "Starting", file: "" };
  if (status.state === "waiting-for-library") return { label: "Waiting for the main disk", file: "" };
  if (status.state === "external-drive-blocked") return { label: "External drive blocked", file: "" };
  if (status.state === "stopped") {
    return { label: `Indexing stopped · ${status.filesIndexed.toLocaleString()} indexed`, file: "" };
  }
  if (status.state === "indexing") {
    const progress = progressLabel(status);
    const prefix = status.priorityFolder && !status.currentFile ? `Indexing ${status.priorityFolder} first` : "Indexing";
    return { label: `${prefix} · ${progress}`, file: status.currentFile || "" };
  }
  const settled = `${status.filesIndexed.toLocaleString()} indexed · ${status.filesSkipped.toLocaleString()} skipped`;
  if (status.state === "watching") return { label: `${settled} · new files index automatically`, file: "" };
  return { label: settled, file: "" };
}

function contentUrl(path) {
  return `/content?path=${encodeURIComponent(path)}`;
}

function parentFolder(path) {
  const slash = path.lastIndexOf("/");
  return slash === -1 ? "" : path.slice(0, slash);
}

function kindLabel(hit) {
  if (hit.fileType === "image") return "Photo";
  const ext = hit.name.includes(".") ? hit.name.split(".").pop().toUpperCase() : "File";
  return ext;
}

function pageSuffix(hit) {
  if (!hit.page) return "";
  const ext = hit.name.includes(".") ? hit.name.split(".").pop().toLowerCase() : "";
  if (!["pdf", "ppt", "pptx", "odp"].includes(ext)) return "";
  return ` · page ${hit.page}`;
}

export function App() {
  const reduce = useReducedMotion();
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("");
  const [priority, setPriority] = useState("");
  const [fileType, setFileType] = useState("ALL");
  const [status, setStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [indexAction, setIndexAction] = useState(false);
  const [selected, setSelected] = useState(0);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await gql(STATUS);
        if (!stop) {
          setStatus(data.status);
          setStatusError("");
        }
      } catch (err) {
        if (!stop) setStatusError(err.message);
      }
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (!query.trim()) {
      setResult(null);
      setSearching(false);
      setSearchError("");
      return undefined;
    }
    let stop = false;
    setSearching(true);
    const timer = setTimeout(async () => {
      try {
        const folder = scope.trim();
        const data = await gql(SEARCH, {
          q: query,
          fileType,
          folder: folder || null,
          limit: 20,
        });
        if (!stop) {
          setResult(data.search);
          setSelected(0);
          setSearchError("");
        }
      } catch (err) {
        if (!stop) setSearchError(err.message);
      } finally {
        if (!stop) setSearching(false);
      }
    }, 180);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [query, fileType, scope]);

  const hits = result?.hits ?? [];
  const current = hits[selected] ?? hits[0] ?? null;

  useEffect(() => {
    function onKeyDown(event) {
      const tag = event.target?.tagName;
      const typingElsewhere = tag === "INPUT" && event.target.getAttribute("aria-label") !== "Search files";
      if (typingElsewhere || tag === "TEXTAREA") return;
      if (event.key === "Escape") {
        setQuery("");
        return;
      }
      if (!hits.length) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSelected((index) => Math.min(hits.length - 1, index + 1));
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setSelected((index) => Math.max(0, index - 1));
      } else if (event.key === "Enter" && tag !== "BUTTON" && tag !== "SUMMARY") {
        event.preventDefault();
        const hit = hits[selected] ?? hits[0];
        if (hit) window.open(contentUrl(hit.path), "_blank", "noreferrer");
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [hits, selected]);

  const busy = status?.state === "indexing" || status?.state === "waiting-for-library" || Boolean(status?.priorityFolder);

  async function refreshStatus() {
    const data = await gql(STATUS);
    setStatus(data.status);
  }

  async function toggleIndexing() {
    if (!status || indexAction) return;
    setIndexAction(true);
    try {
      await gql(status.state === "stopped" ? RESUME_INDEXING : STOP_INDEXING);
      await refreshStatus();
      setStatusError("");
    } catch (err) {
      setStatusError(err.message);
    } finally {
      setIndexAction(false);
    }
  }

  async function submitPriority(event) {
    event.preventDefault();
    const path = priority.trim();
    if (!path) return;
    try {
      await gql(PRIORITIZE, { path });
      setPriority("");
      setStatusError("");
      const data = await gql(STATUS);
      setStatus(data.status);
    } catch (err) {
      setStatusError(err.message);
    }
  }

  const parts = statusParts(status);
  const canToggle = status?.state === "indexing" || status?.state === "watching" || status?.state === "stopped";
  const motionProps = useMemo(
    () =>
      reduce
        ? {}
        : {
            initial: { opacity: 0, y: 8 },
            animate: { opacity: 1, y: 0 },
            exit: { opacity: 0, y: -4 },
          },
    [reduce],
  );
  const shownError = searchError || statusError;

  return (
    <main className={current ? "app with-preview" : "app"}>
      <header className="top">
        <div>
          <h1 className="wordmark">Search</h1>
          <p className="lede">Notes, PDFs, office files, and photos on the main disk.</p>
        </div>
        <p className="status" aria-live="polite">
          <motion.span
            className={busy ? "dot busy" : "dot"}
            animate={reduce || !busy ? undefined : { opacity: [0.35, 1, 0.35] }}
            transition={{ repeat: Infinity, duration: 1.6 }}
          />
          <span className="status-copy">
            {parts.file ? (
              <span className="status-file" title={parts.file}>
                {parts.file}
              </span>
            ) : null}
            <span className="status-counts">{parts.label}</span>
          </span>
          {canToggle ? (
            <motion.button
              type="button"
              className="index-toggle"
              onClick={toggleIndexing}
              disabled={indexAction}
              whileTap={reduce ? undefined : { scale: 0.96 }}
              aria-label={status.state === "stopped" ? "Resume indexing" : "Stop indexing"}
            >
              {status.state === "stopped" ? "Resume" : "Stop"}
            </motion.button>
          ) : null}
        </p>
        {status?.state === "indexing" && typeof status.progress === "number" ? (
          <div
            className="library-progress"
            role="progressbar"
            aria-valuenow={status.progress}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Library indexed"
          >
            <motion.span
              animate={{ width: `${status.progress}%` }}
              transition={reduce ? { duration: 0 } : { duration: 0.45, ease: "easeOut" }}
            />
          </div>
        ) : null}
      </header>

      <label className="searchbox">
        <span aria-hidden="true">⌕</span>
        <input
          type="search"
          aria-label="Search files"
          value={query}
          placeholder="roof leak, red bike, invoice…"
          onChange={(event) => setQuery(event.target.value)}
          autoFocus
        />
        {query ? (
          <button type="button" className="clear" onClick={() => setQuery("")} aria-label="Clear search">
            Clear
          </button>
        ) : null}
      </label>

      <div className="toolbar">
        <div className="filters" role="tablist">
          {FILTERS.map((filter) => (
            <button
              key={filter.id}
              type="button"
              className={fileType === filter.id ? "active" : ""}
              onClick={() => setFileType(filter.id)}
              aria-pressed={fileType === filter.id}
            >
              {filter.label}
            </button>
          ))}
        </div>
        <label className="scope">
          <span>In</span>
          <input
            aria-label="Limit search to a folder"
            placeholder="any folder"
            value={scope}
            onChange={(event) => setScope(event.target.value)}
          />
        </label>
      </div>

      <p className="meta">
        {shownError ? (
          <span className="error">{shownError}</span>
        ) : searching ? (
          "Searching…"
        ) : result ? (
          `${result.hits.length} ${result.hits.length === 1 ? "result" : "results"} · ${result.tookMs} ms`
        ) : (
          "↑↓ to move · enter to open · esc to clear"
        )}
      </p>
      {status?.lastError && status.state === "external-drive-blocked" ? (
        <p className="error">{status.lastError}</p>
      ) : null}

      <div className="workspace">
        <section className="results" aria-label="Results">
          {!query.trim() ? (
            <p className="empty">Type a name, a sentence, or what a photo shows.</p>
          ) : null}
          {query.trim() && result && hits.length === 0 && !searching ? (
            <p className="empty">
              {scope.trim()
                ? `Nothing matches in ${scope.trim()}.`
                : "Nothing matches. Try fewer words, or switch the filter."}
            </p>
          ) : null}
          <AnimatePresence mode="popLayout">
            {hits.map((hit, index) => (
              <motion.a
                className={index === selected ? "result selected" : "result"}
                key={hit.path}
                href={contentUrl(hit.path)}
                target="_blank"
                rel="noreferrer"
                aria-current={index === selected ? "true" : undefined}
                onMouseEnter={() => setSelected(index)}
                onFocus={() => setSelected(index)}
                {...motionProps}
              >
                {hit.fileType === "image" ? (
                  <img className="thumb" alt="" src={contentUrl(hit.path)} />
                ) : (
                  <div className="thumb placeholder" aria-hidden="true">
                    {kindLabel(hit).slice(0, 3)}
                  </div>
                )}
                <div className="result-copy">
                  <div className="result-title">
                    <h2>{highlight(hit.name, query)}</h2>
                    <span className="kind">{kindLabel(hit)}</span>
                    {typeof hit.similarity === "number" ? (
                      <span className="confidence" title="Similarity">
                        {Math.round(hit.similarity * 100)}%
                      </span>
                    ) : null}
                  </div>
                  <p className="path">
                    <span>
                      {parentFolder(hit.path) || "Main disk"}
                      {pageSuffix(hit)}
                    </span>
                  </p>
                  <p className="snippet">{highlight(hit.snippet, query)}</p>
                </div>
              </motion.a>
            ))}
          </AnimatePresence>
        </section>

        <AnimatePresence>
          {current ? (
            <motion.aside
              className="preview"
              key={current.path}
              initial={reduce ? false : { opacity: 0, x: 12 }}
              animate={{ opacity: 1, x: 0 }}
              exit={reduce ? undefined : { opacity: 0, x: 8 }}
              transition={{ duration: 0.18 }}
            >
              {current.fileType === "image" ? (
                <img className="preview-image" alt={current.name} src={contentUrl(current.path)} />
              ) : (
                <div className="preview-mark" aria-hidden="true">
                  {kindLabel(current).slice(0, 4)}
                </div>
              )}
              <p className="kind">{kindLabel(current)}</p>
              <h2>{current.name}</h2>
              <p className="path">{current.path}{pageSuffix(current)}</p>
              <p className="snippet">{highlight(current.snippet, query)}</p>
              <a className="open" href={contentUrl(current.path)} target="_blank" rel="noreferrer">
                Open file
              </a>
            </motion.aside>
          ) : null}
        </AnimatePresence>
      </div>

      <details className="index-tools">
        <summary>Index a folder first</summary>
        <form className="priority" onSubmit={submitPriority}>
          <input
            aria-label="Folder to index first"
            placeholder="notes or photos/2024"
            value={priority}
            onChange={(event) => setPriority(event.target.value)}
          />
          <button type="submit">Queue</button>
        </form>
        <p className="hint">
          {status?.priorityFolder
            ? `Next up: ${status.priorityFolder}`
            : "The walk keeps going. This folder is indexed on the next files, then the scan continues."}
        </p>
      </details>
    </main>
  );
}
