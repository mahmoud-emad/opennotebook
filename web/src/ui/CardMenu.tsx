import { useState, type MouseEvent } from "react";
import { Icon } from "./Icon";

/** A card's ⋯ menu: the button in its corner and the list it opens. One for
 * every collection card and every output row, so all of them are acted on the
 * same way. A sibling of the card's link, never inside it. */
export function CardMenu({
  label,
  items,
  onPick,
  off = [],
}: {
  /** The name of what it acts on, for "More actions for X". */
  label: string;
  /** Each line: what it says, and whether it destroys something. */
  items: [string, boolean][];
  onPick: (i: number) => void;
  /** Lines that cannot be chosen right now, by index. */
  off?: number[];
}) {
  const [open, setOpen] = useState(false);
  const swallow = (e: MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
  };
  return (
    <div className="dots-wrap">
      <button
        className="dots"
        title="More"
        aria-label={`More actions for ${label}`}
        aria-expanded={open}
        onClick={(e) => {
          swallow(e);
          setOpen(!open);
        }}
      >
        <Icon name="three-dots" />
      </button>
      {open && (
        <>
          {/* Clicking anywhere else closes it, and only closes it. */}
          <div
            className="dots-veil"
            onClick={(e) => {
              swallow(e);
              setOpen(false);
            }}
          />
          <div className="dots-pop" role="menu">
            {items.map(([text, bad], i) => (
              <button
                key={i}
                className={bad ? "bad" : ""}
                role="menuitem"
                disabled={off.includes(i)}
                onClick={(e) => {
                  swallow(e);
                  setOpen(false);
                  onPick(i);
                }}
              >
                {text}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
