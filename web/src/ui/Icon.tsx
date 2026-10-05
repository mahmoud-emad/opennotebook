import { ICONS } from "./icons";

/** One of the studio's icons (Bootstrap Icons), drawn inline so the app needs
 * no icon font and nothing from the network. Decorative: the control it sits
 * in carries the accessible name. */
export function Icon({ name, className = "" }: { name: string; className?: string }) {
  return (
    <svg
      className={`ic ${className}`}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
      dangerouslySetInnerHTML={{ __html: ICONS[name] ?? "" }}
    />
  );
}
