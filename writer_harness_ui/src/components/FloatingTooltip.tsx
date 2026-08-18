import { ReactNode, RefObject, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

interface FloatingTooltipProps {
  anchorRef: RefObject<HTMLElement | null>;
  visible: boolean;
  children: ReactNode;
  className?: string;
  offset?: number;
  onMouseEnter?: () => void;
  onMouseLeave?: () => void;
}

export function FloatingTooltip({ anchorRef, visible, children, className = "", offset = 8, onMouseEnter, onMouseLeave }: FloatingTooltipProps) {
  const tooltipRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ top: number; left: number; ready: boolean }>({ top: -9999, left: -9999, ready: false });

  useLayoutEffect(() => {
    if (!visible) {
      setPosition({ top: -9999, left: -9999, ready: false });
      return;
    }

    const updatePosition = () => {
      const anchor = anchorRef.current;
      const tooltip = tooltipRef.current;
      if (!anchor || !tooltip) return;

      const anchorRect = anchor.getBoundingClientRect();
      const tooltipRect = tooltip.getBoundingClientRect();
      const viewportPadding = 12;

      let top = anchorRect.top - tooltipRect.height - offset;
      if (top < viewportPadding) {
        top = Math.min(window.innerHeight - tooltipRect.height - viewportPadding, anchorRect.bottom + offset);
      }

      const centeredLeft = anchorRect.left + (anchorRect.width / 2) - (tooltipRect.width / 2);
      const left = Math.min(
        Math.max(viewportPadding, centeredLeft),
        window.innerWidth - tooltipRect.width - viewportPadding,
      );

      setPosition({ top, left, ready: true });
    };

    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [anchorRef, offset, visible]);

  if (!visible || typeof document === "undefined") return null;

  return createPortal(
    <div
      ref={tooltipRef}
      className={`floating-tooltip-layer ${className}`.trim()}
      role="tooltip"
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      onWheel={(event) => {
        const tooltip = event.currentTarget;
        if (tooltip.scrollHeight <= tooltip.clientHeight) return;
        event.preventDefault();
        event.stopPropagation();
        tooltip.scrollTop += event.deltaY;
      }}
      style={{
        top: `${position.top}px`,
        left: `${position.left}px`,
        visibility: position.ready ? "visible" : "hidden",
      }}
    >
      {children}
    </div>,
    document.body,
  );
}
