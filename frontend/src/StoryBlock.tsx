import { useEffect, useRef, useState } from 'react';
import type { ReactNode, RefObject } from 'react';

interface StoryBlockProps {
  scrollRef: RefObject<HTMLDivElement | null>;
  estimatedHeight: number;
  containsHit: boolean;
  initiallyVisible: boolean;
  renderAll: boolean;
  children: () => ReactNode;
}

/** Keep nearby dialogue mounted while retaining the measured space of distant text. */
export default function StoryBlock({ scrollRef, estimatedHeight, containsHit, initiallyVisible, renderAll, children }: StoryBlockProps) {
  const elementRef = useRef<HTMLDivElement>(null);
  const measuredHeight = useRef(estimatedHeight);
  const [nearViewport, setNearViewport] = useState(initiallyVisible);
  const [focused, setFocused] = useState(false);
  const mounted = renderAll || containsHit || nearViewport || focused;

  useEffect(() => {
    if (renderAll) return;
    const element = elementRef.current;
    if (!element || !('IntersectionObserver' in window)) {
      setNearViewport(true);
      return;
    }
    const observer = new IntersectionObserver(([entry]) => setNearViewport(entry.isIntersecting), {
      root: scrollRef.current,
      rootMargin: '1000px 0px',
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [scrollRef, renderAll]);

  useEffect(() => {
    const element = elementRef.current;
    if (!element || !mounted) return;
    measuredHeight.current = element.getBoundingClientRect().height;
    if (!('ResizeObserver' in window)) return;
    const observer = new ResizeObserver(() => {
      measuredHeight.current = element.getBoundingClientRect().height;
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [mounted]);

  return <div
    ref={elementRef}
    className="story-block"
    data-mounted={mounted}
    style={mounted ? undefined : { height: measuredHeight.current }}
    onFocusCapture={() => setFocused(true)}
    onBlurCapture={event => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false);
    }}
  >{mounted ? children() : null}</div>;
}
