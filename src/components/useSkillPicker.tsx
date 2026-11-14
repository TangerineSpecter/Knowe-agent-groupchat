import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import type { Editor } from '@tiptap/react';
import { useTranslation } from 'react-i18next';
import { callableSkills, type CallableSkill } from '../store/extensions';
import { PLATFORM_PROJECT_ID } from '../store/avatar';
import './extensions.css';

export function useSkillPicker(editor: Editor | null, projectId: string | null | undefined) {
  const { t } = useTranslation();
  const enabled = !!projectId && projectId !== PLATFORM_PROJECT_ID;
  const [query, setQuery] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<CallableSkill[]>([]);
  const [byProject, setByProject] = useState<Record<string, CallableSkill[]>>({});
  const [index, setIndex] = useState(0);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const range = useRef<{ from: number; to: number } | null>(null);
  const popup = useRef<HTMLDivElement | null>(null);
  const [popupStyle, setPopupStyle] = useState<CSSProperties | undefined>(undefined);
  const selected = enabled ? byProject[projectId || ''] || [] : [];
  const open = enabled && query !== null;
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (event.target instanceof Node && !popup.current?.contains(event.target) && !editor?.view.dom.contains(event.target)) setQuery(null);
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open, editor]);
  useEffect(() => {
    setQuery(null); setCatalog([]); setError('');
  }, [projectId]);
  useEffect(() => {
    if (!open || !projectId) return;
    let current = true;
    setLoading(true); setError('');
    void callableSkills(projectId).then(rows => { if (current) setCatalog(rows); })
      .catch(e => { if (current) setError(String(e)); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [open, projectId]);
  useEffect(() => {
    if (!editor || !enabled) return;
    const sync = (): void => {
      const { $from, from, empty } = editor.state.selection;
      const text = $from.parent.textBetween(0, $from.parentOffset, '\n', '\ufffc');
      const match = /(?:^|\s)\/([^\s/]{0,64})$/.exec(text);
      if (!empty || editor.isActive('codeBlock') || !match) { setQuery(null); range.current = null; return; }
      const needle = match[1] || '';
      range.current = { from: from - needle.length - 1, to: from };
      setQuery(needle); setIndex(0);
    };
    editor.on('update', sync); editor.on('selectionUpdate', sync);
    return () => { editor.off('update', sync); editor.off('selectionUpdate', sync); };
  }, [editor, enabled]);
  const rows = useMemo(() => catalog.filter(row => !selected.some(item => item.pack_id === row.pack_id)
    && `${row.name} ${row.description}`.toLocaleLowerCase().includes((query || '').toLocaleLowerCase())), [catalog, selected, query]);

  useLayoutEffect(() => {
    if (!open) { setPopupStyle(undefined); return; }
    const position = (): void => {
      const popupEl = popup.current;
      const parentEl = (popupEl?.offsetParent as HTMLElement | null) ?? popupEl?.parentElement;
      if (!popupEl || !parentEl) return;
      const parentRect = parentEl.getBoundingClientRect();
      if (parentRect.width === 0 && parentRect.height === 0) return;

      const cardRect = (parentEl.closest('.chat-card') as HTMLElement | null)?.getBoundingClientRect();
      const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
      const margin = 12;
      const gap = 8;
      const visibleTop = Math.max(margin, (cardRect?.top ?? 0) + margin);
      const visibleBottom = Math.min(viewportHeight - margin, (cardRect?.bottom ?? viewportHeight) - margin);
      const natural = Math.min(320, popupEl.scrollHeight || 320);

      let trigger: { top: number; bottom: number } | null = null;
      if (editor) {
        try {
          const coords = editor.view.coordsAtPos(editor.state.selection.$from.pos);
          if (coords && (coords.top > 0 || coords.bottom > 0)) trigger = { top: coords.top, bottom: coords.bottom };
        } catch { trigger = null; }
      }
      if (!trigger) trigger = { top: parentRect.top, bottom: parentRect.bottom };

      const spaceAboveWrap = Math.max(0, parentRect.top - visibleTop - 6);
      const spaceAboveTrigger = Math.max(0, trigger.top - visibleTop - gap);
      const spaceBelowTrigger = Math.max(0, visibleBottom - trigger.bottom - gap);

      let next: CSSProperties;
      if (spaceAboveWrap >= natural || (spaceAboveWrap >= 160 && spaceAboveWrap >= spaceBelowTrigger)) {
        next = { maxHeight: Math.floor(Math.min(320, spaceAboveWrap)) };
      } else if (spaceBelowTrigger >= natural || spaceBelowTrigger >= spaceAboveTrigger) {
        next = {
          top: Math.round(trigger.bottom + gap - parentRect.top),
          bottom: 'auto',
          maxHeight: Math.floor(Math.min(320, spaceBelowTrigger)),
        };
      } else {
        next = {
          top: 'auto',
          bottom: Math.round(parentRect.bottom - trigger.top + gap),
          maxHeight: Math.floor(Math.min(320, spaceAboveTrigger)),
        };
      }
      setPopupStyle(prev => (prev?.top === next.top && prev?.bottom === next.bottom && prev?.maxHeight === next.maxHeight ? prev : next));
    };
    position();
    window.addEventListener('resize', position);
    const parentEl = (popup.current?.offsetParent as HTMLElement | null) ?? popup.current?.parentElement;
    const observer = parentEl && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(position) : null;
    if (observer && parentEl) observer.observe(parentEl);
    return () => { window.removeEventListener('resize', position); observer?.disconnect(); };
  }, [open, editor, loading, error, rows.length, query]);
  const pick = useCallback((skill: CallableSkill): void => {
    if (!editor || !range.current || !projectId) return;
    if ((byProject[projectId]?.length || 0) >= 8) { setError(t('extensions.skillLimit')); return; }
    const active = range.current;
    setByProject(current => ({ ...current, [projectId]: [...(current[projectId] || []).filter(row => row.pack_id !== skill.pack_id), skill] }));
    editor.chain().focus().deleteRange(active).run();
    setQuery(null);
  }, [editor, projectId, byProject, t]);
  const keyDown = (event: KeyboardEvent): boolean => {
    if (!open || event.isComposing) return false;
    if (event.key === 'Escape') { event.preventDefault(); setQuery(null); return true; }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (rows.length) setIndex(value => (value + (event.key === 'ArrowDown' ? 1 : rows.length - 1)) % rows.length);
      return true;
    }
    if ((event.key === 'Enter' && !event.ctrlKey && !event.metaKey) || event.key === 'Tab') {
      const row = rows[Math.min(index, rows.length - 1)];
      if (row) { event.preventDefault(); pick(row); return true; }
    }
    return false;
  };
  const clear = (): void => {
    if (projectId) setByProject(current => ({ ...current, [projectId]: [] }));
    setQuery(null);
  };
  const ui = <>
    {open && <div ref={popup} className="skill-picker" style={popupStyle} role="listbox" aria-label={t('extensions.chooseSkill')}>
      {loading ? <p role="status">{t('extensions.loadingSkills')}</p> : error ? <p className="extension-error" role="alert">{error}</p>
        : rows.length === 0 ? <p>{t('extensions.noSkills')}</p>
          : rows.map((row, i) => <button type="button" role="option" aria-selected={i === index} key={row.pack_id}
            onMouseDown={event => event.preventDefault()} onClick={() => pick(row)}>
            <strong>/{row.name}</strong><span>{row.description}</span></button>)}
    </div>}
    {selected.length > 0 && <div className="skill-selections" aria-label={t('extensions.selectedSkills')}>
      {selected.map(row => <button type="button" key={row.pack_id} title={t('extensions.removeSkill', { name: row.name })}
        onClick={() => projectId && setByProject(current => ({ ...current, [projectId]: selected.filter(item => item.pack_id !== row.pack_id) }))}>
        /{row.name}<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="m3 3 6 6m0-6-6 6" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>
      </button>)}
    </div>}
  </>;
  return { selected, clear, keyDown, ui };
}
