/** Keep the actual session controls together without duplicating their handlers. */
export function installSessionActions(head, trigger) {
  const panel = document.createElement('div');
  panel.className = 'aw-session-actions';
  panel.setAttribute('popover', 'auto');
  panel.setAttribute('role', 'group');
  panel.setAttribute('aria-label', 'Session actions');
  for (const button of [...head.querySelectorAll('button')]) {
    const label = document.createElement('span');
    label.textContent = button.getAttribute('aria-label');
    button.append(label);
    panel.append(button);
  }
  head.append(trigger, panel);
  trigger.setAttribute('aria-expanded', 'false');
  const place = () => {
    const rect = trigger.getBoundingClientRect();
    panel.style.top = `${rect.bottom + 6}px`;
    panel.style.right = `${Math.max(8, window.innerWidth - rect.right)}px`;
    panel.style.maxHeight = `${Math.max(80, window.innerHeight - rect.bottom - 14)}px`;
  };
  const items = () => [...panel.querySelectorAll('button')].filter(b => !b.hidden && !b.disabled);
  const open = () => { place(); panel.showPopover(); };
  trigger.addEventListener('click', () => {
    if (panel.matches(':popover-open')) panel.hidePopover();
    else open();
  });
  trigger.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault(); open();
    (event.key === 'ArrowDown' ? items()[0] : items().at(-1))?.focus();
  });
  panel.addEventListener('toggle', () => trigger.setAttribute('aria-expanded', String(panel.matches(':popover-open'))));
  // Close before an action opens a modal, so focus stays in that modal.
  panel.addEventListener('click', event => {
    if (event.target.closest('button') && panel.matches(':popover-open')) panel.hidePopover();
  }, true);
  panel.addEventListener('keydown', event => {
    const buttons = items();
    const index = buttons.indexOf(document.activeElement);
    const next = {ArrowDown: (index + 1) % buttons.length, ArrowUp: (index - 1 + buttons.length) % buttons.length, Home: 0, End: buttons.length - 1}[event.key];
    if (next !== undefined) { event.preventDefault(); buttons[next]?.focus(); }
    if (event.key === 'Escape') { event.preventDefault(); panel.hidePopover(); trigger.focus(); }
  });
  window.addEventListener('resize', place);
  return () => { panel.remove(); window.removeEventListener('resize', place); };
}
