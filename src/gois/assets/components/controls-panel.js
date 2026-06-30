/**
 * Controls Panel Component
 * Modern, accessible dropdown and button component
 */

class ControlsPanel {
  constructor(container = document.body) {
    this.container = container;
    this.init();
  }

  init() {
    this.setupToggle();
    this.setupDropdowns();
    this.setupEventDelegation();
  }

  /**
   * Setup collapse/expand toggle
   */
  setupToggle() {
    const toggleBtn = document.getElementById('toggle-btn');
    const content = document.getElementById('controls-content');

    if (!toggleBtn || !content) return;

    toggleBtn.addEventListener('click', () => {
      const isExpanded = toggleBtn.getAttribute('aria-expanded') === 'true';
      const newState = !isExpanded;

      toggleBtn.setAttribute('aria-expanded', String(newState));
      content.classList.toggle('collapsed', !newState);

      // Store preference
      localStorage.setItem('controls-panel-expanded', String(newState));
    });

    // Restore previous state
    const wasExpanded = localStorage.getItem('controls-panel-expanded') !== 'false';
    if (!wasExpanded) {
      toggleBtn.click();
    }
  }

  /**
   * Setup all dropdown selectors
   */
  setupDropdowns() {
    const dropdowns = document.querySelectorAll('.select-dropdown');

    dropdowns.forEach(dropdown => {
      const button = dropdown.querySelector('.select-button');
      const menu = dropdown.querySelector('.select-menu');
      const options = dropdown.querySelectorAll('.select-option');

      if (!button || !menu) return;

      // Toggle menu visibility
      button.addEventListener('click', (e) => {
        e.stopPropagation();
        this.toggleMenu(button, menu);
      });

      // Handle option selection
      options.forEach(option => {
        option.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          this.selectOption(dropdown, option, button, menu);
        });
      });

      // Close menu on outside click
      document.addEventListener('click', (e) => {
        if (!dropdown.contains(e.target) && menu.hasAttribute('hidden') === false) {
          menu.setAttribute('hidden', '');
          button.setAttribute('aria-expanded', 'false');
        }
      });

      // Keyboard navigation
      button.addEventListener('keydown', (e) => {
        this.handleKeyboardNav(e, button, menu, options);
      });
    });
  }

  /**
   * Toggle dropdown menu visibility
   */
  toggleMenu(button, menu) {
    const isOpen = !menu.hasAttribute('hidden');

    if (isOpen) {
      menu.setAttribute('hidden', '');
      button.setAttribute('aria-expanded', 'false');
    } else {
      menu.removeAttribute('hidden');
      button.setAttribute('aria-expanded', 'true');
      // Focus first option
      const firstOption = menu.querySelector('.select-option');
      if (firstOption) firstOption.focus();
    }
  }

  /**
   * Handle option selection
   */
  selectOption(dropdown, option, button, menu) {
    // Update UI
    const label = button.querySelector('.select-button-label');
    label.textContent = option.textContent.trim();

    // Update selected state
    const prevSelected = menu.querySelector('.select-option.selected');
    if (prevSelected) prevSelected.classList.remove('selected');
    option.classList.add('selected');

    // Close menu
    menu.setAttribute('hidden', '');
    button.setAttribute('aria-expanded', 'false');

    // Dispatch custom event
    const event = new CustomEvent('optionSelected', {
      detail: {
        value: option.textContent.trim(),
        dropdown: dropdown
      }
    });
    dropdown.dispatchEvent(event);
  }

  /**
   * Handle keyboard navigation in dropdown
   */
  handleKeyboardNav(e, button, menu, options) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      this.toggleMenu(button, menu);
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (menu.hasAttribute('hidden')) {
        menu.removeAttribute('hidden');
        button.setAttribute('aria-expanded', 'true');
      }
      const firstOption = menu.querySelector('.select-option');
      if (firstOption) firstOption.focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      menu.setAttribute('hidden', '');
      button.setAttribute('aria-expanded', 'false');
      button.focus();
    }
  }

  /**
   * Setup event delegation for buttons
   */
  setupEventDelegation() {
    this.container.addEventListener('click', (e) => {
      const btn = e.target.closest('.btn');
      if (!btn) return;

      // Add visual feedback
      this.activateButton(btn);

      // Dispatch custom event
      const event = new CustomEvent('buttonClicked', {
        detail: {
          text: btn.textContent.trim(),
          button: btn
        }
      });
      btn.dispatchEvent(event);
    });
  }

  /**
   * Visual feedback for button activation
   */
  activateButton(btn) {
    btn.style.transform = 'scale(0.95)';
    setTimeout(() => {
      btn.style.transform = '';
    }, 100);
  }

  /**
   * Get current values
   */
  getValues() {
    const values = {};
    const dropdowns = document.querySelectorAll('.select-dropdown');

    dropdowns.forEach(dropdown => {
      const label = dropdown.querySelector('.select-label').textContent;
      const value = dropdown.querySelector('.select-button-label').textContent;
      values[label.toLowerCase()] = value;
    });

    return values;
  }

  /**
   * Set dropdown value programmatically
   */
  setValue(label, value) {
    const dropdowns = document.querySelectorAll('.select-dropdown');

    dropdowns.forEach(dropdown => {
      const labelEl = dropdown.querySelector('.select-label');
      if (labelEl.textContent.toLowerCase() !== label.toLowerCase()) return;

      const menu = dropdown.querySelector('.select-menu');
      const options = menu.querySelectorAll('.select-option');

      options.forEach(option => {
        if (option.textContent.trim() === value) {
          const button = dropdown.querySelector('.select-button');
          this.selectOption(dropdown, option, button, menu);
        }
      });
    });
  }
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    window.controlsPanel = new ControlsPanel();
  });
} else {
  window.controlsPanel = new ControlsPanel();
}

// Export for use in modules
if (typeof module !== 'undefined' && module.exports) {
  module.exports = ControlsPanel;
}
