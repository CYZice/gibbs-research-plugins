const navLinks = [...document.querySelectorAll('.side-nav a[href^="#"]')];
const sections = [...document.querySelectorAll('.intro, .doc-section, .tool-section')];

if ('IntersectionObserver' in window) {
  const observer = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;

      const id = `#${visible.target.id}`;
      navLinks.forEach((link) => {
        link.classList.toggle('active', link.getAttribute('href') === id);
      });
    },
    { rootMargin: '-20% 0px -62% 0px', threshold: [0.15, 0.35, 0.6] },
  );

  sections.forEach((section) => observer.observe(section));
}
