"use client";

import { useState } from "react";

export default function ExpandableText({
  text,
  previewChars = 500,
}: {
  text: string;
  previewChars?: number;
}) {
  const [expanded, setExpanded] = useState(false);

  if (text.length <= previewChars) {
    return <pre className="json">{text}</pre>;
  }

  return (
    <div>
      <pre className="json">{expanded ? text : `${text.slice(0, previewChars)}…`}</pre>
      <button className="secondary" onClick={() => setExpanded((v) => !v)}>
        {expanded ? "Show less" : `Show all (${text.length.toLocaleString()} chars)`}
      </button>
    </div>
  );
}
