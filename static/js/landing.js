(function () {
  const menu = document.querySelector('.menu-toggle');
  const nav = document.querySelector('.nav-links');
  const siteNav = document.querySelector('.site-nav');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  if (menu && nav) {
    menu.addEventListener('click', function () {
      const open = nav.classList.toggle('open');
      menu.setAttribute('aria-expanded', String(open));
    });
    nav.querySelectorAll('a').forEach((link) => link.addEventListener('click', () => {
      nav.classList.remove('open');
      menu.setAttribute('aria-expanded', 'false');
    }));
  }

  const updateNav = () => siteNav && siteNav.classList.toggle('is-scrolled', window.scrollY > 24);
  updateNav();
  window.addEventListener('scroll', updateNav, { passive: true });

  if (reducedMotion || !('IntersectionObserver' in window)) {
    document.querySelectorAll('.reveal').forEach((item) => item.classList.add('is-visible'));
  } else {
    const reveal = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          reveal.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -4% 0px' });
    document.querySelectorAll('.reveal').forEach((item) => reveal.observe(item));
  }

  if (!reducedMotion && window.matchMedia('(pointer: fine)').matches) {
    let pointerFrame = 0;
    window.addEventListener('pointermove', (event) => {
      if (pointerFrame) return;
      pointerFrame = window.requestAnimationFrame(() => {
        document.body.style.setProperty('--pointer-x', `${event.clientX}px`);
        document.body.style.setProperty('--pointer-y', `${event.clientY}px`);
        pointerFrame = 0;
      });
    }, { passive: true });

    document.querySelectorAll('.liquid-surface').forEach((surface) => {
      surface.addEventListener('pointermove', (event) => {
        surface.classList.remove('glow-settle');
        const rect = surface.getBoundingClientRect();
        surface.style.setProperty('--mx', `${event.clientX - rect.left}px`);
        surface.style.setProperty('--my', `${event.clientY - rect.top}px`);
      }, { passive: true });
      surface.addEventListener('pointerleave', () => {
        surface.classList.add('glow-settle');
        surface.style.setProperty('--mx', '30%');
        surface.style.setProperty('--my', '15%');
      });
    });

    document.querySelectorAll('[data-tilt]').forEach((card) => {
      let tiltFrame = 0;
      card.addEventListener('pointermove', (event) => {
        if (tiltFrame) return;
        tiltFrame = window.requestAnimationFrame(() => {
          const rect = card.getBoundingClientRect();
          const x = (event.clientX - rect.left) / rect.width - 0.5;
          const y = (event.clientY - rect.top) / rect.height - 0.5;
          card.style.setProperty('--tilt-x', `${(-y * 3).toFixed(2)}deg`);
          card.style.setProperty('--tilt-y', `${(x * 4).toFixed(2)}deg`);
          tiltFrame = 0;
        });
      }, { passive: true });
      card.addEventListener('pointerleave', () => {
        card.style.setProperty('--tilt-x', '0deg');
        card.style.setProperty('--tilt-y', '0deg');
      });
    });
  }

  document.querySelectorAll('.faq-item').forEach((item) => {
    const summary = item.querySelector('summary');
    const panel = item.querySelector('.faq-panel');
    if (!summary || !panel) return;
    summary.addEventListener('click', (event) => {
      event.preventDefault();
      if (item.classList.contains('is-animating')) return;
      if (reducedMotion) {
        item.open = !item.open;
        return;
      }
      item.classList.add('is-animating');
      if (!item.open) {
        item.open = true;
        const target = panel.scrollHeight;
        panel.style.height = '0px';
        window.requestAnimationFrame(() => {
          panel.style.height = `${target}px`;
        });
        panel.addEventListener('transitionend', function onEnd(e) {
          if (e.propertyName !== 'height') return;
          panel.style.height = '';
          item.classList.remove('is-animating');
          panel.removeEventListener('transitionend', onEnd);
        });
      } else {
        panel.style.height = `${panel.scrollHeight}px`;
        window.requestAnimationFrame(() => {
          panel.style.height = '0px';
        });
        panel.addEventListener('transitionend', function onEnd(e) {
          if (e.propertyName !== 'height') return;
          item.open = false;
          panel.style.height = '';
          item.classList.remove('is-animating');
          panel.removeEventListener('transitionend', onEnd);
        });
      }
    });
  });

  const SCROLL_KEY = 'takt-scroll-y';
  const LANG_KEY = 'takt-lang-switch';
  try {
    if (sessionStorage.getItem(LANG_KEY)) {
      sessionStorage.removeItem(LANG_KEY);
      const y = parseInt(sessionStorage.getItem(SCROLL_KEY) || '0', 10);
      sessionStorage.removeItem(SCROLL_KEY);
      window.scrollTo({ top: y, left: 0, behavior: 'instant' });
      window.requestAnimationFrame(() => {
        document.documentElement.classList.remove('lang-pending');
      });
    }
  } catch (e) {
    document.documentElement.classList.remove('lang-pending');
  }

  const calc = document.querySelector('.tariff-calc');
  if (calc) {
    const slider = calc.querySelector('.calc-slider');
    const pointsOut = calc.querySelector('.calc-points-value');
    const discountOut = calc.querySelector('.calc-discount');
    const totalOut = calc.querySelector('.calc-total');
    const planButtons = calc.querySelectorAll('.calc-plan');
    const locale = document.documentElement.lang === 'en' ? 'en-US' : 'ru-RU';
    let pricePerPoint = Number(calc.querySelector('.calc-plan.is-active')?.dataset.price || planButtons[0]?.dataset.price || 0);

    const discountFor = (points) => {
      if (points >= 10) return 0.20;
      if (points >= 5) return 0.12;
      if (points >= 2) return 0.05;
      return 0;
    };

    const render = () => {
      const points = Number(slider.value);
      const discount = discountFor(points);
      const base = pricePerPoint * points;
      const total = Math.round(base * (1 - discount));
      pointsOut.textContent = String(points);
      discountOut.textContent = `${Math.round(discount * 100)}%`;
      totalOut.textContent = `${total.toLocaleString(locale)} ₽`;
    };

    slider.addEventListener('input', render);
    planButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        planButtons.forEach((b) => b.classList.remove('is-active'));
        btn.classList.add('is-active');
        pricePerPoint = Number(btn.dataset.price || 0);
        render();
      });
    });
    render();
  }

  const pcconceptCode = document.getElementById('pcconcept-code');
  if (pcconceptCode) {
    const statusText = document.getElementById('pcconcept-status');
    const successEl = document.getElementById('pcconcept-success');
    const openAppBtn = document.getElementById('pcconcept-open-app');
    const openFeedback = document.getElementById('pcconcept-open-feedback');
    const VALID_LENGTHS = [4, 6, 8];

    const setStatus = (state) => {
      pcconceptCode.classList.remove('is-invalid', 'is-valid');
      statusText.classList.remove('is-invalid', 'is-valid');
      if (state === 'invalid') {
        pcconceptCode.classList.add('is-invalid');
        statusText.classList.add('is-invalid');
        statusText.textContent = pcconceptCode.dataset.invalidText;
        pcconceptCode.setAttribute('aria-invalid', 'true');
      } else if (state === 'valid') {
        pcconceptCode.classList.add('is-valid');
        statusText.classList.add('is-valid');
        statusText.textContent = pcconceptCode.dataset.validText;
        pcconceptCode.setAttribute('aria-invalid', 'false');
      } else {
        statusText.textContent = pcconceptCode.dataset.hint;
        pcconceptCode.setAttribute('aria-invalid', 'false');
      }
    };

    const showSuccess = (show) => {
      if (show) {
        successEl.hidden = false;
        window.requestAnimationFrame(() => successEl.classList.add('is-visible'));
      } else {
        successEl.classList.remove('is-visible');
        successEl.hidden = true;
        openFeedback.hidden = true;
      }
    };

    pcconceptCode.addEventListener('input', () => {
      const digits = pcconceptCode.value.replace(/\D/g, '').slice(0, 8);
      if (pcconceptCode.value !== digits) pcconceptCode.value = digits;

      if (!digits.length) {
        setStatus('empty');
        showSuccess(false);
        return;
      }
      if (VALID_LENGTHS.includes(digits.length)) {
        setStatus('valid');
        showSuccess(true);
        return;
      }
      // Still mid-typing toward a valid length — no error yet, just hide
      // any previous success/error state until they finish or blur.
      pcconceptCode.classList.remove('is-invalid', 'is-valid');
      statusText.classList.remove('is-invalid', 'is-valid');
      statusText.textContent = pcconceptCode.dataset.hint;
      showSuccess(false);
    });

    pcconceptCode.addEventListener('blur', () => {
      const digits = pcconceptCode.value;
      if (digits.length && !VALID_LENGTHS.includes(digits.length)) {
        setStatus('invalid');
      }
    });

    pcconceptCode.addEventListener('focus', () => {
      if (pcconceptCode.classList.contains('is-invalid')) {
        setStatus('empty');
      }
    });

    if (openAppBtn && openFeedback) {
      openAppBtn.addEventListener('click', () => {
        openFeedback.textContent = openAppBtn.dataset.clickedText;
        openFeedback.hidden = false;
      });
    }
  }

  document.querySelectorAll('.language-switch a').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (link.classList.contains('active')) return;
      const href = link.getAttribute('href');
      if (!href) return;
      event.preventDefault();
      try {
        sessionStorage.setItem(SCROLL_KEY, String(window.scrollY));
        sessionStorage.setItem(LANG_KEY, '1');
      } catch (e) {
        window.location.href = href;
        return;
      }
      if (reducedMotion) {
        window.location.href = href;
        return;
      }
      document.body.style.opacity = '0';
      window.setTimeout(() => {
        window.location.href = href;
      }, 180);
    });
  });

  const cookieBanner = document.getElementById('cookie-banner');
  if (cookieBanner) {
    const COOKIE_ACK_KEY = 'takt-cookie-notice-ack';
    let acknowledged = false;
    try {
      acknowledged = localStorage.getItem(COOKIE_ACK_KEY) === '1';
    } catch (e) {
      acknowledged = false;
    }
    if (!acknowledged) {
      cookieBanner.hidden = false;
      window.requestAnimationFrame(() => {
        cookieBanner.classList.add('is-visible');
      });
    }
    const dismissBtn = document.getElementById('cookie-banner-dismiss');
    if (dismissBtn) {
      dismissBtn.addEventListener('click', () => {
        try {
          localStorage.setItem(COOKIE_ACK_KEY, '1');
        } catch (e) {
          // Local storage unavailable (e.g. private browsing) — the banner
          // will simply show again next visit, which is a safe fallback.
        }
        cookieBanner.classList.remove('is-visible');
        window.setTimeout(() => {
          cookieBanner.hidden = true;
        }, reducedMotion ? 0 : 350);
      });
    }
  }
})();
