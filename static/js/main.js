// TechBid Marketplace - main.js

function escapeHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function restoreAutoDisabledButtons(scope = document) {
  scope.querySelectorAll('button[data-auto-disabled="1"]').forEach((btn) => {
    btn.disabled = false;
    if (btn.dataset.originalText) {
      btn.textContent = btn.dataset.originalText;
    }
    delete btn.dataset.autoDisabled;
  });
}

function renderNotificationList(container, notifications) {
  if (!container) return;
  if (!notifications.length) {
    container.innerHTML = `
      <div class="empty-state">
        <p>No notifications yet.</p>
      </div>
    `;
    return;
  }

  container.innerHTML = notifications.map((item) => `
    <div class="notif-item">
      <div class="notif-dot"></div>
      <div class="notif-content">
        ${item.target_url
          ? `<a href="${escapeHtml(item.target_url)}" class="notif-link">${escapeHtml(item.message)}</a>`
          : `<div>${escapeHtml(item.message)}</div>`}
        <div class="notif-time">${escapeHtml(item.created_at_human || 'just now')}</div>
      </div>
    </div>
  `).join('');
}

function renderNotificationBadge(unreadCount) {
  const navNotif = document.querySelector('.nav-notif');
  if (!navNotif) return;

  let badge = document.getElementById('nav-notif-badge');
  if (unreadCount > 0) {
    if (!badge) {
      badge = document.createElement('span');
      badge.id = 'nav-notif-badge';
      badge.className = 'notif-badge';
      navNotif.appendChild(badge);
    }
    badge.textContent = String(unreadCount);
  } else if (badge) {
    badge.remove();
  }
}

function initLiveNotifications() {
  const notificationsApi = document.body.dataset.notificationsApi;
  const containers = Array.from(document.querySelectorAll('[data-live-notifications]'));
  if (!notificationsApi) return;
  if (!document.querySelector('.nav-notif') && !containers.length) return;

  let timer = null;

  async function refreshNotifications() {
    if (document.visibilityState !== 'visible') {
      timer = window.setTimeout(refreshNotifications, 15000);
      return;
    }

    const maxLimit = containers.length
      ? Math.max(...containers.map((container) => Number(container.dataset.limit || 5)), 5)
      : 5;
    const markRead = containers.some((container) => container.dataset.markRead === '1') ? '1' : '0';

    try {
      const response = await fetch(`${notificationsApi}?limit=${maxLimit}&mark_read=${markRead}`, {
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      });
      if (!response.ok) throw new Error('Unable to refresh notifications');
      const payload = await response.json();
      const notifications = Array.isArray(payload.notifications) ? payload.notifications : [];

      renderNotificationBadge(Number(payload.unread_count || 0));
      containers.forEach((container) => {
        const limit = Number(container.dataset.limit || notifications.length || 5);
        renderNotificationList(container, notifications.slice(0, limit));
      });
    } catch (err) {
      console.error('Notification refresh error:', err);
    } finally {
      timer = window.setTimeout(refreshNotifications, 15000);
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      refreshNotifications();
    }
  });

  refreshNotifications();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (e) => {
      if (!confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', () => {
      setTimeout(() => {
        form.querySelectorAll('button[type=submit]').forEach((btn) => {
          if (btn.disabled) return;
          if (!btn.dataset.originalText) btn.dataset.originalText = btn.textContent;
          btn.disabled = true;
          btn.dataset.autoDisabled = '1';
          btn.textContent = 'Please wait...';
        });
      }, 10);
    });
  });

  initLiveNotifications();

  // Theme Toggle
  const themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    const updateThemeIcon = () => {
      const isDark = document.documentElement.classList.contains('dark-theme');
      themeToggle.textContent = isDark ? '☀️' : '🌙';
      themeToggle.title = isDark ? 'Switch to Light Mode' : 'Switch to Dark Mode';
      themeToggle.setAttribute('aria-label', isDark ? 'Switch to light mode' : 'Switch to dark mode');
    };

    themeToggle.addEventListener('click', () => {
      const isDark = document.documentElement.classList.contains('dark-theme');
      if (isDark) {
        document.documentElement.classList.remove('dark-theme');
        document.body.classList.remove('dark-theme');
        localStorage.setItem('theme', 'light');
        document.cookie = 'theme=light; path=/; max-age=31536000';
      } else {
        document.documentElement.classList.add('dark-theme');
        document.body.classList.add('dark-theme');
        localStorage.setItem('theme', 'dark');
        document.cookie = 'theme=dark; path=/; max-age=31536000';
      }
      updateThemeIcon();
    });

    updateThemeIcon();
  }
});

window.addEventListener('pageshow', () => {
  restoreAutoDisabledButtons();
});
