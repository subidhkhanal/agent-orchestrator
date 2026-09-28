"use client";

import React from "react";

type Source = { id: string; title: string; url_or_doc_ref: string; snippet: string };

// Minimal markdown: headings, bullets, paragraphs, **bold**, and [src_x] citations that link
// to the source list. Deliberately small: no HTML injection path, everything is React text.
function inline(text: string, sources: Map<string, Source>, keyPrefix: string) {
  const parts = text.split(/(\[(?:src|doc)_[0-9a-f]{6,}\]|\*\*[^*]+\*\*)/g);
  return parts.map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    const cite = part.match(/^\[((?:src|doc)_[0-9a-f]{6,})\]$/);
    if (cite) {
      const src = sources.get(cite[1]);
      const n = src ? Array.from(sources.keys()).indexOf(cite[1]) + 1 : "?";
      return (
        <a key={key} href={`#${cite[1]}`} className={src ? "cite" : "cite bad"} title={src?.title ?? "unknown source"}>
          [{n}]
        </a>
      );
    }
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={key}>{part.slice(2, -2)}</strong>;
    return <React.Fragment key={key}>{part}</React.Fragment>;
  });
}

export default function Memo({ content, sources }: { content: string; sources: Source[] }) {
  const byId = new Map(sources.map((s) => [s.id, s]));
  const blocks: React.ReactNode[] = [];
  let list: React.ReactNode[] = [];
  const flush = () => {
    if (list.length) blocks.push(<ul key={`ul-${blocks.length}`}>{list}</ul>);
    list = [];
  };
  content.split("\n").forEach((raw, i) => {
    const line = raw.trimEnd();
    if (/^[-*] /.test(line)) {
      list.push(<li key={i}>{inline(line.slice(2), byId, `l${i}`)}</li>);
      return;
    }
    flush();
    if (!line.trim()) return;
    const h = line.match(/^(#{1,3}) (.*)$/);
    if (h) {
      const Tag = (`h${h[1].length + 2}`) as "h3" | "h4" | "h5";
      blocks.push(<Tag key={i}>{inline(h[2], byId, `h${i}`)}</Tag>);
    } else {
      blocks.push(<p key={i}>{inline(line, byId, `p${i}`)}</p>);
    }
  });
  flush();
  return (
    <div className="memo">
      {blocks}
      {sources.length > 0 && (
        <ol className="sources">
          {sources.map((s) => (
            <li key={s.id} id={s.id}>
              {s.url_or_doc_ref.startsWith("http") ? (
                <a href={s.url_or_doc_ref} target="_blank" rel="noreferrer">
                  {s.title}
                </a>
              ) : (
                <span>{s.title}</span>
              )}{" "}
              <code>{s.id}</code>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
