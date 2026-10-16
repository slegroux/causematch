// The theme classes MonsterUI's Theme.headers() set inline, moved out of the page so the CSP
// can forbid inline scripts. Loaded without defer, so the classes are set before first paint.
// One look, "Warm & friendly" (static/theme.css): light only until a dark one is designed.
document.documentElement.classList.add("uk-theme-warm");
