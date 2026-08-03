import * as React from "react"
import * as TabsPrimitive from "@radix-ui/react-tabs"

import { cn } from "@/lib/utils"
import { FOCUS_RING } from "@/components/ui/focus-ring"

/**
 * Console Tabs — shadcn's Radix wrapper re-themed down to the instrument
 * panel's density, and onto this repo's tokens.
 *
 * Two things changed from stock and both are house rules rather than taste.
 * (1) **Density**: stock ships `h-9` / `text-sm`, which is a content-site tab
 * strip; every mount site here is chrome inside an already-dense surface (a log
 * card, a composer popover), so the base is `h-7` / `text-2xs` and a call site
 * no longer has to restate it. (2) **Tokens**: the vendored file used shadcn's
 * bare colour aliases (`bg-muted`, `text-muted-foreground`); every colour in
 * this tree resolves through `hsl(var(--token))` so a theme edit lands in one
 * place. `--surface` is the sunken track the pill rides in, matching the strips
 * `SpawnAgentCard` and `CheckAgentsCard` already hand-rolled at their call
 * sites.
 *
 * Radii follow the shape vocabulary: `rounded-lg` track, `rounded-md` pill.
 * `rounded-full` is reserved for state containers and a tab is not one.
 */

const Tabs = TabsPrimitive.Root

const TabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.List
    ref={ref}
    className={cn(
      "inline-flex h-7 items-center justify-center gap-0.5 rounded-lg bg-[hsl(var(--surface))] p-0.5 text-[hsl(var(--muted-foreground))]",
      className
    )}
    {...props}
  />
))
TabsList.displayName = TabsPrimitive.List.displayName

const TabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Trigger
    ref={ref}
    className={cn(
      "inline-flex h-6 items-center justify-center gap-1 whitespace-nowrap rounded-md px-2 text-2xs font-medium transition-colors hover:text-[hsl(var(--foreground))] disabled:pointer-events-none disabled:opacity-50 data-[state=active]:bg-[hsl(var(--card))] data-[state=active]:text-[hsl(var(--foreground))] data-[state=active]:[box-shadow:var(--elev-1)]",
      FOCUS_RING,
      className
    )}
    {...props}
  />
))
TabsTrigger.displayName = TabsPrimitive.Trigger.displayName

const TabsContent = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content
    ref={ref}
    className={cn("mt-2", FOCUS_RING, className)}
    {...props}
  />
))
TabsContent.displayName = TabsPrimitive.Content.displayName

export { Tabs, TabsList, TabsTrigger, TabsContent }
