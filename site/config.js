// Where the website reads its data: the folder that holds web/*.json (the pipeline writes it on every update).
// Override for testing with ?data=<base url> in the page address, e.g. ?data=/demo-data
window.GASDASH_CONFIG = {
  DATA_BASE: "https://pub-2828285bab12449a8361d29bcb8483fe.r2.dev/eu-gas",
  REFRESH_MINUTES: 15,
  POWER_DASH_URL: "https://amun0905.github.io/gb-power-dash/",
};
