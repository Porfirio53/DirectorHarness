import { FieldBadge } from "./FieldBadge";
import type { KeyValueItem } from "../types";

interface JsonSectionProps {
  title: string;
  content: unknown;
  items?: KeyValueItem[];
  tone?: "default" | "success" | "warning" | "danger";
  columns?: 2 | 3;
}

function renderValue(value: string | string[]) {
  if (Array.isArray(value)) {
    return (
      <div className="kv-list">
        {value.map((item) => (
          <span key={item} className="kv-chip">{item}</span>
        ))}
      </div>
    );
  }
  return <p className="kv-text">{value || "-"}</p>;
}

export function JsonSection({ title, content, items = [], tone = "default", columns = 2 }: JsonSectionProps) {
  return (
    <div className="json-section">
      <div className="json-section__header">
        <FieldBadge label={title} tone={tone} />
      </div>
      {items.length ? (
        <div className={`kv-grid ${columns === 3 ? "kv-grid--triple" : ""}`}>
          {items.map((item) => (
            <div key={item.label} className={`kv-card kv-card--${item.tone || "default"} ${item.span === "full" ? "kv-card--full" : ""}`}>
              <span className="kv-label">{item.label}</span>
              {renderValue(item.value)}
            </div>
          ))}
        </div>
      ) : (
        <pre>{JSON.stringify(content, null, 2)}</pre>
      )}
    </div>
  );
}
