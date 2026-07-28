/* Les épingles de la carte — une seule définition pour toute l'app.
   La rêverie et le carnet doivent montrer la MÊME épingle : c'est la même
   idée, avant et après le voyage.

   Attend, posés par la page avant de charger ce fichier :
     window.PIN_KINDS = [ [kind, emoji, libellé], ... ]
   Expose window.NH_PINS.
*/
(function (global) {
  var DAY_COLORS = ['#A8503D', '#5B7A9D', '#5C7A5A', '#B0713C', '#8C6B7A', '#4A97A8',
                    '#7A7A4F', '#B58373', '#3D4A5C', '#B8864B', '#5E8D87', '#96604F'];

  function emoji(kind) {
    var K = global.PIN_KINDS || [];
    for (var i = 0; i < K.length; i++) if (K[i][0] === kind) return K[i][1];
    return '';
  }

  function libelle(kind) {
    var K = global.PIN_KINDS || [];
    for (var i = 0; i < K.length; i++) if (K[i][0] === kind) return K[i][2];
    return '';
  }

  /* num : rang dans un parcours en cours de tracé ('1', '1·3'…) ou null
     kind : type d'épingle (dormir, manger…)
     jour : index du jour planifié, pour la couleur, ou null
     multi : l'épingle tient plusieurs jours -> liseré pointillé */
  function icon(num, kind, jour, multi) {
    if (!global.L) return null;
    if (num) {
      return L.divIcon({ className: 'pin-icon pin-icon-num',
                         html: '<span>' + num + '</span>',
                         iconSize: [26, 26], iconAnchor: [13, 24] });
    }
    var cls = 'pin-icon';
    if (kind) cls += ' pin-icon-' + kind;
    if (jour != null && jour >= 0) cls += ' pin-day-' + (jour % DAY_COLORS.length);
    if (multi) cls += ' pin-multi';
    var em = emoji(kind);
    return L.divIcon({ className: cls,
                       html: em ? '<span class="pk">' + em + '</span>' : '',
                       iconSize: [28, 28], iconAnchor: [14, 25] });
  }

  /* Épingle d'une photo : la vignette elle-même fait l'icône. */
  function iconPhoto(thumb) {
    if (!global.L) return null;
    return L.divIcon({ className: 'pin-photo',
                       html: '<img src="' + thumb + '" alt="">',
                       iconSize: [34, 34], iconAnchor: [17, 30] });
  }

  /* Le sélecteur d'emoji du popup, partagé lui aussi. */
  function selecteurHtml(selected, idPrefix) {
    var K = global.PIN_KINDS || [];
    return '<div class="pin-kinds" id="' + idPrefix + '-kinds">' + K.map(function (k) {
      return '<button type="button" data-kind="' + k[0] + '" title="' + k[2] + '"' +
        (k[0] === selected ? ' class="is-active"' : '') +
        ' onclick="NH_PINS.choisir(this)">' + k[1] + '</button>';
    }).join('') + '</div>';
  }

  function choisir(btn) {
    btn.parentElement.querySelectorAll('button').forEach(function (b) {
      b.classList.remove('is-active');
    });
    btn.classList.add('is-active');
  }

  global.NH_PINS = {
    DAY_COLORS: DAY_COLORS,
    emoji: emoji, libelle: libelle,
    icon: icon, iconPhoto: iconPhoto,
    selecteurHtml: selecteurHtml, choisir: choisir
  };
})(window);
