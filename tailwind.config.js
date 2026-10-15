// Tailwind 3.4.17, run by scripts/build_css.sh: the same setup MonsterUI gave the Play CDN
// (darkMode 'selector', nothing else), compiled ahead of time into static/vendor/app.css.
module.exports = {
  darkMode: "selector",
  content: [
    "./main.py",
    "./app/**/*.py",
    "./static/*.js",
    // MonsterUI's components carry their own Tailwind classes
    `${process.env.MONSTERUI_DIR}/*.py`,
  ],
};
