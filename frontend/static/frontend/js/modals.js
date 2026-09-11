/*
 * Formulaires en modal, chargés en AJAX.
 *
 * Tout bouton portant [data-modal-url] charge le fragment HTML de cette URL
 * dans le modal partagé #modal-formulaire. La soumission du formulaire
 * injecté est interceptée : en cas de succès le serveur redirige (fetch la
 * suit automatiquement, on navigue vers response.url) ; en cas d'erreur de
 * validation, le serveur renvoie le même fragment avec les erreurs, qui
 * remplace simplement le contenu du modal — jamais de rechargement de page.
 */
(function () {
  function contenu() {
    return document.getElementById('modal-formulaire-contenu');
  }

  function instanceModal() {
    return bootstrap.Modal.getOrCreateInstance(document.getElementById('modal-formulaire'));
  }

  function injecter(html) {
    var conteneur = contenu();
    conteneur.innerHTML = html;
    // Les <script> injectés via innerHTML n'exécutent jamais automatiquement
    // (particularité du DOM) : on les recrée pour qu'ils s'exécutent
    // réellement (utile pour le préremplissage de certains formulaires).
    conteneur.querySelectorAll('script').forEach(function (ancien) {
      var nouveau = document.createElement('script');
      Array.from(ancien.attributes).forEach(function (attr) {
        nouveau.setAttribute(attr.name, attr.value);
      });
      nouveau.textContent = ancien.textContent;
      ancien.replaceWith(nouveau);
    });
  }

  function attacherFormulaire() {
    var form = contenu().querySelector('form');
    if (!form) {
      return;
    }
    form.addEventListener('submit', function (evt) {
      evt.preventDefault();
      var action = form.getAttribute('action') || window.location.href;
      fetch(action, {
        method: 'POST',
        body: new FormData(form),
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      }).then(function (reponse) {
        if (reponse.redirected) {
          window.location = reponse.url;
          return null;
        }
        return reponse.text();
      }).then(function (html) {
        if (html === null || html === undefined) {
          return;
        }
        injecter(html);
        attacherFormulaire();
      }).catch(function () {
        // Le plus souvent une session expirée : le fetch ne peut pas suivre
        // la redirection cross-origin vers Keycloak (bloquée par CORS). Un
        // rechargement complet, lui, suit la vraie redirection de connexion.
        window.location.reload();
      });
    });
  }

  function chargerModal(url) {
    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (reponse) { return reponse.text(); })
      .then(function (html) {
        injecter(html);
        attacherFormulaire();
        instanceModal().show();
      })
      .catch(function () {
        window.location.reload();
      });
  }

  document.addEventListener('click', function (evt) {
    var declencheur = evt.target.closest('[data-modal-url]');
    if (!declencheur) {
      return;
    }
    evt.preventDefault();
    chargerModal(declencheur.getAttribute('data-modal-url'));
  });
})();
