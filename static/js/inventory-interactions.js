(() => {
  const usable = (field) => !field.disabled && field.getClientRects().length > 0;

  for (const form of document.querySelectorAll('[data-enter-order]')) {
    const names = form.dataset.enterOrder.split(' ');
    form.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' || event.defaultPrevented || event.isComposing) return;
      if (!names.includes(event.target.name)) return;

      const fields = Array.from(form.elements).filter(usable);
      const sequence = names.map((name) => fields.find((field) => field.name === name));
      sequence.push(fields.find((field) => field.type === 'submit'));
      const next = sequence.slice(names.indexOf(event.target.name) + 1).find(Boolean);
      if (next) {
        event.preventDefault();
        next.focus();
      }
    });
  }

  // Every other form: Enter in a text-like field moves to the next field instead of submitting;
  // on the last field it submits as usual. Search and filter forms keep their plain behaviour.
  const TEXT_LIKE = new Set(['text', 'number', 'tel', 'email', 'password', 'date', 'month', 'time', 'url']);
  const focusable = (field) => {
    if (field.disabled || field.readOnly || field.getClientRects().length === 0) return false;
    if (field.tagName === 'SELECT' || field.tagName === 'TEXTAREA') return true;
    return field.tagName === 'INPUT' && TEXT_LIKE.has(field.type);
  };
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.defaultPrevented || event.isComposing) return;
    if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
    const field = event.target;
    if (field.tagName !== 'SELECT' && (field.tagName !== 'INPUT' || !TEXT_LIKE.has(field.type))) return;
    const form = field.form;
    if (!form || form.method === 'get' || form.matches('[role="search"], [data-enter-order], [data-no-enter-next]')) return;
    const fields = Array.from(form.elements).filter(focusable);
    const next = fields[fields.indexOf(field) + 1];
    if (!next) {
      if (field.tagName === 'SELECT') {
        event.preventDefault();
        form.requestSubmit();
      }
      return;
    }
    event.preventDefault();
    next.focus();
    if (typeof next.select === 'function' && next.tagName === 'INPUT') next.select();
  });

  // Whole-number fields (e.g. the material minimum): no decimal point, exponent or sign,
  // and a lone 0 disappears when the field is entered so the user can type straight away.
  document.addEventListener('keydown', (event) => {
    if (!event.target.matches?.('input[data-whole-number]')) return;
    if (['.', ',', 'e', 'E', '-', '+'].includes(event.key)) event.preventDefault();
  });
  document.addEventListener('focusin', (event) => {
    const field = event.target;
    if (field.matches?.('input[data-whole-number]') && Number(field.value) === 0 && field.value !== '') field.value = '';
  });
  document.addEventListener('beforeinput', (event) => {
    if (!event.target.matches?.('input[data-whole-number]')) return;
    if (!event.data || !/\D/.test(event.data)) return;
    // Typed, pasted or dropped text: keep only its digits ("2.5" -> "25", "12 dona" -> "12").
    event.preventDefault();
    const digits = event.data.replace(/\D/g, '');
    if (digits) {
      const field = event.target;
      field.value = field.value + digits;
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });

  // Phone inputs ([data-phone-mask]): digits are shown as "+998 90 123 45 67" while typing,
  // on phones and computers alike. Text with letters (an old username on the login form) is
  // left exactly as typed. The server normalises any spelling, so this is only for readability.
  const PHONE_LOCAL_DIGITS = 9;
  const phoneLocalDigits = (value) => {
    let digits = value.replace(/\D/g, '');
    if (digits.startsWith('998')) digits = digits.slice(3);
    // A full number pasted after the "+998 " already in the field brings a second 998.
    if (digits.length > PHONE_LOCAL_DIGITS && digits.startsWith('998')) digits = digits.slice(3);
    if (digits.length === PHONE_LOCAL_DIGITS + 1 && digits.startsWith('0')) digits = digits.slice(1);
    return digits.slice(0, PHONE_LOCAL_DIGITS);
  };
  const formatPhone = (local) => {
    const groups = [local.slice(0, 2), local.slice(2, 5), local.slice(5, 7), local.slice(7, 9)].filter(Boolean);
    return groups.length ? `+998 ${groups.join(' ')}` : '+998 ';
  };
  const looksLikeText = (value) => /[^\d\s+()\-]/.test(value);

  function maskPhone(field, { deleting = false } = {}) {
    const raw = field.value;
    if (!raw || looksLikeText(raw)) return;
    const local = phoneLocalDigits(raw);
    if (!local && deleting) return; // let the user clear the "+998" prefix
    // Keep the caret after the same number of local digits it was after before formatting.
    const caret = field.selectionStart ?? raw.length;
    const before = phoneLocalDigits(raw.slice(0, caret)).length;
    const formatted = formatPhone(local);
    if (formatted === raw) return;
    field.value = formatted;
    let position = formatted.length;
    if (before < local.length) {
      let seen = 0;
      position = 5; // just after "+998 "
      while (position < formatted.length && seen < before) {
        if (/\d/.test(formatted[position])) seen += 1;
        position += 1;
      }
    }
    try { field.setSelectionRange(position, position); } catch (error) { /* type=tel always supports it */ }
  }

  for (const field of document.querySelectorAll('input[data-phone-mask]')) {
    maskPhone(field); // a saved number like +998901234567 is shown spaced out
  }
  document.addEventListener('input', (event) => {
    if (event.target.matches?.('input[data-phone-mask]')) {
      maskPhone(event.target, { deleting: (event.inputType || '').startsWith('delete') });
    }
  });
  document.addEventListener('focusin', (event) => {
    const field = event.target;
    if (field.matches?.('input[data-phone-mask]') && !field.value) {
      field.value = '+998 ';
      try { field.setSelectionRange(5, 5); } catch (error) { /* ignore */ }
    }
  });
  document.addEventListener('focusout', (event) => {
    const field = event.target;
    if (field.matches?.('input[data-phone-mask]') && field.value.trim() === '+998') field.value = '';
  });

  function attachSuggestions(input, getOptions, onSelect, { showOnFocus = false } = {}) {
    const list = document.createElement('div');
    list.id = `${input.id}-suggestions`;
    list.className = 'material-suggestions';
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', input.dataset.suggestionLabel);
    list.hidden = true;
    document.body.append(list);
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', list.id);
    input.setAttribute('aria-expanded', 'false');
    let options = [];
    let active = -1;
    let timer;
    let pending;

    function hide() {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }

    function close() {
      clearTimeout(timer);
      pending?.abort();
      hide();
    }

    function position() {
      const rect = input.getBoundingClientRect();
      const below = window.innerHeight - rect.bottom - 10;
      const above = rect.top - 10;
      const placeBelow = below >= 144 || below >= above;
      list.style.left = `${rect.left}px`;
      list.style.width = `${rect.width}px`;
      list.style.maxHeight = `${Math.min(260, placeBelow ? below : above)}px`;
      list.style.top = placeBelow ? `${rect.bottom + 4}px` : 'auto';
      list.style.bottom = placeBelow ? 'auto' : `${window.innerHeight - rect.top + 4}px`;
    }

    function highlight(index) {
      active = index;
      Array.from(list.children).forEach((option, i) => option.setAttribute('aria-selected', String(index === i)));
      const option = list.children[index];
      input.setAttribute('aria-activedescendant', option.id);
      option.scrollIntoView({ block: 'nearest' });
    }

    function choose(index) {
      const option = options[index];
      if (!option) return;
      close();
      input.value = option.name;
      onSelect(option);
    }

    function refresh() {
      close();
      timer = setTimeout(async () => {
        const request = new AbortController();
        pending = request;
        try {
          const results = await getOptions(input.value.trim(), request.signal);
          if (request.signal.aborted || document.activeElement !== input || !results.length) return;
          options = results;
          list.replaceChildren(...options.map(({ name }, index) => {
            const option = document.createElement('div');
            option.className = 'material-suggestion';
            option.id = `${list.id}-${index}`;
            option.setAttribute('role', 'option');
            option.setAttribute('aria-selected', 'false');
            option.dataset.index = index;
            option.textContent = name;
            return option;
          }));
          position();
          list.hidden = false;
          input.setAttribute('aria-expanded', 'true');
        } catch (error) {
          if (!request.signal.aborted) hide();
        }
      }, 120);
    }

    input.addEventListener('input', refresh);
    if (showOnFocus) input.addEventListener('focus', refresh);
    input.addEventListener('blur', close);
    window.addEventListener('resize', close);
    document.addEventListener('scroll', (event) => {
      if (!list.contains(event.target)) close();
    }, { capture: true, passive: true });
    list.addEventListener('mousedown', (event) => event.preventDefault());
    list.addEventListener('click', (event) => {
      const option = event.target.closest('[role="option"]');
      if (option) choose(Number(option.dataset.index));
    });
    input.addEventListener('keydown', (event) => {
      if (event.isComposing) return;
      if (event.key === 'Escape') {
        if (!list.hidden) event.preventDefault();
        close();
      } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (list.hidden) refresh();
        else {
          const direction = event.key === 'ArrowDown' ? 1 : -1;
          const start = active < 0 && direction < 0 ? 0 : active;
          highlight((start + direction + options.length) % options.length);
        }
      } else if (event.key === 'Enter' && !list.hidden && active >= 0) {
        event.preventDefault();
        choose(active);
      }
    });
  }

  for (const form of document.querySelectorAll('[data-auto-search]')) {
    const input = form.elements.q;
    input.addEventListener('input', () => { form.elements.match.value = ''; });
    attachSuggestions(input, async (query, signal) => {
      if (!query) return [];
      const url = new URL(form.dataset.autoSearch, window.location.origin);
      url.searchParams.set('q', query);
      if (form.elements.category) url.searchParams.set('category', form.elements.category.value);
      const response = await fetch(url, { signal, headers: { Accept: 'application/json' } });
      if (!response.ok) return [];
      const { names } = await response.json();
      return names.map((name) => ({ name }));
    }, () => {
      form.elements.match.value = 'exact';
      form.requestSubmit();
    });
  }

  const exampleData = document.getElementById('material-name-examples');
  if (exampleData) {
    const examples = JSON.parse(exampleData.textContent);
    const normalize = (value) => value.toLocaleLowerCase().replace(/[ʻʼ’‘]/g, "'");
    for (const input of document.querySelectorAll('[data-material-example]')) {
      const categoryField = () => Array.from(input.form.elements).find((field) => field.name === 'category' && usable(field));
      attachSuggestions(input, (query) => {
        const category = categoryField()?.value;
        const entries = category ? [[category, examples[category] || []]] : Object.entries(examples);
        const choices = entries.flatMap(([code, names]) => names.map((name) => ({ name, code })));
        const prefix = normalize(query);
        const matches = choices.filter(({ name }) => normalize(name).startsWith(prefix) && normalize(name) !== prefix);
        return (matches.length || !category || !prefix ? matches : choices).slice(0, 5);
      }, ({ code }) => {
        const category = categoryField();
        if (category && !category.value && Array.from(category.options).some((option) => option.value === code)) {
          category.value = code;
          category.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }, { showOnFocus: true });
    }
  }
})();

// Bell menu (templates/base.html): mark one entry, or all, as read without leaving the page.
document.addEventListener('alpine:init', () => {
  Alpine.data('notificationMenu', () => {
    // Kept in closures: Alpine's magic properties are not available after an await.
    let root;
    const items = () => root.querySelectorAll('[data-note-key]');
    const post = async (url) => {
      const token = document.querySelector('input[name="csrfmiddlewaretoken"]')?.value || '';
      try {
        const response = await fetch(url, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'X-CSRFToken': token, Accept: 'application/json' },
        });
        return response.ok;
      } catch (error) {
        return false;
      }
    };
    return {
      open: false,
      unread: 0,
      init() {
        root = this.$root;
        this.unread = items().length;
      },
      async read(key, event) {
        const item = event.currentTarget.closest('[data-note-key]');
        if (!(await post(root.dataset.readUrl.replace('KEY', key)))) return;
        item.remove();
        this.unread = items().length;
      },
      async readAll() {
        if (!(await post(root.dataset.readAllUrl))) return;
        items().forEach((item) => item.remove());
        this.unread = 0;
      },
    };
  });
});
