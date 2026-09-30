'use strict';

(() => {
  const search = document.querySelector('#search');
  const cards = [...document.querySelectorAll('.app-card')];
  const categories = [...document.querySelectorAll('.nav-item')];
  let category = 'all';
  const normalize = value => value.toLocaleLowerCase('ru').replaceAll('ё', 'е').trim();

  function update() {
    const words = normalize(search.value).split(/\s+/).filter(Boolean);
    let count = 0;
    for (const card of cards) {
      const matches = (category === 'all' || card.dataset.category === category)
        && words.every(word => normalize(card.dataset.search).includes(word));
      card.hidden = !matches;
      if (matches) count++;
    }
    document.querySelector('#result-count').textContent = count;
    document.querySelector('#empty-state').hidden = count !== 0;
    const label = categories.find(button => button.dataset.category === category).dataset.label;
    document.querySelector('#search-status').textContent = `${label}. Найдено приложений: ${count}`;
  }

  for (const button of categories) {
    button.addEventListener('click', () => {
      category = button.dataset.category;
      for (const other of categories) {
        const selected = other === button;
        other.classList.toggle('active', selected);
        other.setAttribute('aria-pressed', String(selected));
      }
      update();
    });
  }
  search.addEventListener('input', update);
  document.querySelector('#reset-search').addEventListener('click', () => {
    search.value = '';
    categories[0].click();
    search.focus();
  });
  document.addEventListener('keydown', event => {
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey
        && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)
        && !document.activeElement.isContentEditable) {
      event.preventDefault();
      search.focus();
    }
    if (event.key === 'Escape' && document.activeElement === search) {
      search.value = '';
      update();
    }
  });
})();
