interface FieldBadgeProps {
  label: string;
  tone?: "default" | "success" | "warning" | "danger";
}

export function FieldBadge({ label, tone = "default" }: FieldBadgeProps) {
  return <span className={`field-badge field-badge--${tone}`}>{label}</span>;
}
