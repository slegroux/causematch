// Tailwind 3.4.17, run by scripts/build_css.sh: the setup MonsterUI gave the Play CDN
// (darkMode 'selector'), compiled ahead of time into static/vendor/app.css, plus the
// "Warm & friendly" tokens static/theme.css defines.
const colors = require("tailwindcss/colors");

const token = (name) => `hsl(var(--cm-${name}) / <alpha-value>)`;
const cause = (name) => ({
  DEFAULT: token(name),
  soft: token(`${name}-soft`),
  ink: token(`${name}-ink`),
});
const font = ["Nunito", "ui-rounded", "ui-sans-serif", "system-ui", "sans-serif"];

module.exports = {
  darkMode: "selector",
  content: [
    "./main.py",
    "./app/**/*.py",
    "./static/*.js",
    // MonsterUI's components carry their own Tailwind classes
    `${process.env.MONSTERUI_DIR}/*.py`,
  ],
  // every cause color, used or not yet: a page may pick one by slug
  safelist: [{ pattern: /^(bg|text|border)-cause-(food|parks|animals|homeless)(-soft|-ink)?$/ }],
  theme: {
    extend: {
      colors: {
        // MonsterUI's muted text is text-gray-500: warm grays instead of cool ones
        gray: colors.stone,
        cream: token("cream"),
        sand: token("sand"),
        line: token("line"),
        ink: { DEFAULT: token("ink"), muted: token("ink-muted") },
        brand: {
          DEFAULT: token("brand"),
          strong: token("brand-strong"),
          soft: token("brand-soft"),
          ink: token("brand-ink"),
        },
        // bg-cause-food, bg-cause-food-soft, text-cause-food-ink, ...
        cause: {
          food: cause("food"),
          parks: cause("parks"),
          animals: cause("animals"),
          homeless: cause("homeless"),
        },
      },
      fontFamily: { sans: font, display: font },
      boxShadow: {
        warm: "0 1px 2px 0 rgb(45 30 23 / 0.04), 0 10px 28px -12px rgb(45 30 23 / 0.18)",
      },
    },
  },
};
