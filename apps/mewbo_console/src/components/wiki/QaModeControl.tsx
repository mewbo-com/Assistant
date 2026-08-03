/**
 * Fast/deep toggle for the Q&A composer — a sibling of `ModelPicker`
 * (`variant="compact"`), reusing the same trigger token set (`h-7`,
 * `rounded-full`, `--card` bg, `hover:bg-accent`) so it reads as the same
 * control family. A `DropdownMenuRadioGroup` over a two-row table, the same
 * idiom `SearchScopeControl`'s tier picker uses — no `toggle-group` primitive
 * is vendored, and a two-option choice doesn't need one.
 */

import { ChevronDown, Network, Zap } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

import type { QaMode } from "./api/types";

const MODES: { id: QaMode; name: string; hint: string }[] = [
  { id: "fast", name: "Fast", hint: "Direct retrieval, quicker — no probe fan-out." },
  { id: "deep", name: "Deep", hint: "Hypervisor + probe fan-out, more thorough." },
];

interface QaModeControlProps {
  value: QaMode;
  onChange: (mode: QaMode) => void;
  className?: string;
}

export function QaModeControl({ value, onChange, className }: QaModeControlProps) {
  const current = MODES.find((m) => m.id === value) ?? MODES[0];

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          title="Q&A mode — how the answer is retrieved"
          aria-label="Q&A mode"
          className={cn(
            "inline-flex items-center gap-1.5 px-2.5 h-7 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-2xs text-[hsl(var(--foreground))] whitespace-nowrap hover:bg-[hsl(var(--accent))] transition-colors",
            className,
          )}
        >
          {current.id === "fast" ? (
            <Zap className="h-3 w-3 shrink-0 text-[hsl(var(--muted-foreground))]" />
          ) : (
            <Network className="h-3 w-3 shrink-0 text-[hsl(var(--muted-foreground))]" />
          )}
          <span className="truncate">{current.name}</span>
          <ChevronDown className="h-3 w-3 text-[hsl(var(--muted-foreground))] shrink-0" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        collisionPadding={16}
        className="w-64 [box-shadow:var(--elev-3)]"
      >
        <DropdownMenuLabel className="text-2xs font-normal text-[hsl(var(--muted-foreground))]">
          Q&A mode
        </DropdownMenuLabel>
        <DropdownMenuRadioGroup value={value} onValueChange={(v) => onChange(v as QaMode)}>
          {MODES.map((m) => (
            <DropdownMenuRadioItem key={m.id} value={m.id} className="py-1.5">
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="font-medium">{m.name}</span>
                <span className="text-2xs text-[hsl(var(--muted-foreground))]">{m.hint}</span>
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
