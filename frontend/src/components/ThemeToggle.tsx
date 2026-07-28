import { Moon, Sun } from "lucide-react";
import { useTheme } from "../theme";
import { cx } from "./ui";

export default function ThemeToggle({ className }: { className?: string }) {
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === "dark";
  const label = isDark ? "Switch to light theme" : "Switch to night theme";
  const Icon = isDark ? Sun : Moon;

  return (
    <button
      type="button"
      className={cx("icon-button", "theme-toggle", className)}
      onClick={toggleTheme}
      aria-label={label}
      title={label}
    >
      <Icon size={18} aria-hidden="true" />
    </button>
  );
}
