from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from frontend.mixins import permission_requise, queryset_tenant, querystring_sans_page
from recouvrement.api.v1.serializers import TypeContratSerializer
from recouvrement.models import TypeContrat


@permission_requise('recouvrement.view_typecontrat')
def liste(request):
    qs = queryset_tenant(TypeContrat, request.user)

    recherche = request.GET.get('q', '').strip()
    if recherche:
        qs = qs.filter(Q(libelle__icontains=recherche) | Q(code__icontains=recherche))

    page = Paginator(qs.order_by('libelle'), 25).get_page(request.GET.get('page'))

    return render(request, 'frontend/types_contrat/list.html', {
        'page_obj': page,
        'recherche': recherche,
        'querystring': querystring_sans_page(request),
        'peut_modifier': request.user.has_perm('recouvrement.change_typecontrat'),
    })


@permission_requise('recouvrement.add_typecontrat')
def creer(request):
    if request.method == 'POST':
        serializer = TypeContratSerializer(data=request.POST.copy(), context={'request': request})
        if serializer.is_valid():
            serializer.save(compte_id=request.user.compte_id)
            messages.success(request, f"Type de contrat {serializer.instance.libelle} créé.")
            return redirect('frontend:types-contrat-liste')
        return render(request, 'frontend/types_contrat/_modal_form.html', {
            'erreurs': serializer.errors, 'valeurs': request.POST, 'type_contrat': None,
        })

    return render(request, 'frontend/types_contrat/_modal_form.html', {
        'erreurs': {}, 'valeurs': {}, 'type_contrat': None,
    })


@permission_requise('recouvrement.change_typecontrat')
def modifier(request, pk):
    type_contrat = get_object_or_404(queryset_tenant(TypeContrat, request.user), pk=pk)

    if request.method == 'POST':
        serializer = TypeContratSerializer(
            type_contrat, data=request.POST.copy(), context={'request': request}, partial=True,
        )
        if serializer.is_valid():
            serializer.save()
            messages.success(request, "Type de contrat mis à jour.")
            return redirect('frontend:types-contrat-liste')
        return render(request, 'frontend/types_contrat/_modal_form.html', {
            'erreurs': serializer.errors, 'valeurs': request.POST, 'type_contrat': type_contrat,
        })

    return render(request, 'frontend/types_contrat/_modal_form.html', {
        'erreurs': {},
        'valeurs': {
            'libelle': type_contrat.libelle,
            'code': type_contrat.code,
            'est_principal': type_contrat.est_principal,
        },
        'type_contrat': type_contrat,
    })
