import type { ReactNode } from "react";

/**
 * Renders the `<b>...</b>`-highlighted snippet the backend's `ts_headline`
 * call returns (see sense_tool/app/services/search.py) as real React
 * elements - not via dangerouslySetInnerHTML.
 *
 * ts_headline only wraps matched terms in <b>; it does NOT HTML-escape the
 * surrounding document text, and that text originates from OCR'd uploads
 * (i.e. not something this app should trust as safe HTML). So this parses
 * just the `<b>`/`</b>` vocabulary itself and renders everything else as
 * plain text - the highlight markup shows up as bold, and nothing else in
 * the snippet can ever be interpreted as markup.
 */
export function renderHighlightedSnippet(snippet: string): ReactNode[] {
  const parts = snippet.split(/(<b>|<\/b>)/g);
  const nodes: ReactNode[] = [];
  let bold = false;
  let key = 0;

  for (const part of parts) {
    if (part === "<b>") {
      bold = true;
      continue;
    }
    if (part === "</b>") {
      bold = false;
      continue;
    }
    if (part === "") continue;
    nodes.push(
      bold ? (
        <strong className="highlight" key={key++}>
          {part}
        </strong>
      ) : (
        <span key={key++}>{part}</span>
      ),
    );
  }

  return nodes;
}
