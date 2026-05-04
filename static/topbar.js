// Topbar — toggle du menu d'espace + fermeture sur clic externe
(function(){
  var btn = document.getElementById('espace-toggle');
  var menu = document.getElementById('espace-menu');
  if (!btn || !menu) return;
  btn.addEventListener('click', function(e){
    e.stopPropagation();
    var hidden = menu.hidden;
    menu.hidden = !hidden;
    btn.setAttribute('aria-expanded', hidden ? 'true' : 'false');
  });
  document.addEventListener('click', function(e){
    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) {
      menu.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && !menu.hidden) {
      menu.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    }
  });
})();
