import logging

from django.conf import settings
from django.db.models import Q
from django.db.models.query import EmptyQuerySet
from django.http import HttpResponse, HttpResponseForbidden
from rest_framework import mixins, status
from rest_framework.generics import RetrieveAPIView, UpdateAPIView, get_object_or_404, DestroyAPIView
from django.shortcuts import get_list_or_404
from rest_framework.response import Response
from concepts.models import ConceptVersion
from concepts.serializers import ConceptVersionDetailSerializer
from mappings.models import Mapping
from mappings.serializers import MappingDetailSerializer
from oclapi.mixins import ListWithHeadersMixin
from oclapi.permissions import HasAccessToVersionedObject, CanEditConceptDictionaryVersion, CanViewConceptDictionary, CanViewConceptDictionaryVersion, CanEditConceptDictionary
from oclapi.views import ResourceVersionMixin, ResourceAttributeChildMixin, ConceptDictionaryUpdateMixin, ConceptDictionaryCreateMixin, ConceptDictionaryExtrasView, ConceptDictionaryExtraRetrieveUpdateDestroyView, parse_updated_since_param, parse_boolean_query_param
from sources.filters import SourceSearchFilter
from sources.models import Source, SourceVersion
from sources.serializers import SourceCreateSerializer, SourceListSerializer, SourceDetailSerializer, SourceVersionDetailSerializer, SourceVersionListSerializer, SourceVersionCreateSerializer, SourceVersionUpdateSerializer
from tasks import export_source
from celery_once import AlreadyQueued
from users.models import UserProfile
from orgs.models import Organization

INCLUDE_CONCEPTS_PARAM = 'includeConcepts'
INCLUDE_MAPPINGS_PARAM = 'includeMappings'
LIMIT_PARAM = 'limit'
INCLUDE_RETIRED_PARAM = 'includeRetired'
OFFSET_PARAM = 'offset'
DEFAULT_OFFSET = 0

logger = logging.getLogger('oclapi')


class SourceBaseView():
    lookup_field = 'source'
    pk_field = 'mnemonic'
    model = Source
    permission_classes = (CanViewConceptDictionary,)
    queryset = Source.objects.filter(is_active=True)

    def get_detail_serializer(self, obj, data=None, files=None, partial=False):
        return SourceDetailSerializer(obj, data, files, partial)


class SourceRetrieveUpdateDestroyView(SourceBaseView,
                                      ConceptDictionaryUpdateMixin,
                                      RetrieveAPIView,
                                      DestroyAPIView):
    serializer_class = SourceDetailSerializer

    def initialize(self, request, path_info_segment, **kwargs):
        if request.method in ['GET', 'HEAD']:
            self.permission_classes = (CanViewConceptDictionary,)
        else:
            self.permission_classes = (CanEditConceptDictionary,)
        super(SourceRetrieveUpdateDestroyView, self).initialize(request, path_info_segment, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        super(SourceRetrieveUpdateDestroyView, self).retrieve(request, *args, **kwargs)
        self.object = self.get_object()
        serializer = self.get_serializer(self.object)
        data = serializer.data

        source_version = None
        offset = request.QUERY_PARAMS.get(OFFSET_PARAM, DEFAULT_OFFSET)
        try:
            offset = int(offset)
        except ValueError:
            offset = DEFAULT_OFFSET
        limit = settings.REST_FRAMEWORK.get('MAX_PAGINATE_BY', self.paginate_by)
        include_retired = False
        include_concepts = request.QUERY_PARAMS.get(INCLUDE_CONCEPTS_PARAM, False)
        include_mappings = request.QUERY_PARAMS.get(INCLUDE_MAPPINGS_PARAM, False)
        updated_since = None
        if include_concepts or include_mappings:
            source_version = SourceVersion.get_latest_version_of(self.object)
            paginate_by = self.get_paginate_by(EmptyQuerySet())
            if paginate_by:
                limit = min(limit, paginate_by)
            include_retired = request.QUERY_PARAMS.get(INCLUDE_RETIRED_PARAM, False)
            updated_since = parse_updated_since_param(request)

        if include_concepts:
            source_version_concepts = source_version.concepts
            queryset = ConceptVersion.objects.filter(is_active=True)
            if not include_retired:
                queryset = queryset.filter(~Q(retired=True))
            if updated_since:
                queryset = queryset.filter(updated_at__gte=updated_since)
            queryset = queryset.filter(id__in=source_version_concepts)
            queryset = queryset[offset:offset+limit]
            serializer = ConceptVersionDetailSerializer(queryset, many=True)
            data['concepts'] = serializer.data

        if include_mappings:
            all_children = source_version.mappings
            queryset = Mapping.objects.filter(is_active=True)
            if not include_retired:
                queryset = queryset.filter(~Q(retired=True))
            queryset = queryset.filter(id__in=all_children)
            if updated_since:
                queryset = queryset.filter(updated_at__gte=updated_since)
            queryset = queryset[offset:offset+limit]
            serializer = MappingDetailSerializer(queryset, many=True)
            data['mappings'] = serializer.data

        return Response(data)


class SourceListView(SourceBaseView,
                     ConceptDictionaryCreateMixin,
                     ListWithHeadersMixin):
    serializer_class = SourceCreateSerializer
    filter_backends = [SourceSearchFilter]
    solr_fields = {
        'sourceType': {'sortable': False, 'filterable': True, 'facet': True},
        'name': {'sortable': True, 'filterable': False},
        'lastUpdate': {'sortable': True, 'filterable': False},
        'locale': {'sortable': False, 'filterable': True, 'facet': True},
        'owner': {'sortable': False, 'filterable': True, 'facet': True},
        'ownerType': {'sortable': False, 'filterable': True, 'facet': True},
    }

    def get(self, request, *args, **kwargs):
        self.serializer_class = SourceDetailSerializer if self.is_verbose(request) else SourceListSerializer
        return self.list(request, *args, **kwargs)


class SourceExtrasView(ConceptDictionaryExtrasView):
    pass


class SourceExtraRetrieveUpdateDestroyView(ConceptDictionaryExtraRetrieveUpdateDestroyView):
    concept_dictionary_version_class = SourceVersion


RELEASED_PARAM = 'released'
PROCESSING_PARAM = 'processing'


class SourceVersionBaseView(ResourceVersionMixin):
    lookup_field = 'version'
    pk_field = 'mnemonic'
    model = SourceVersion
    queryset = SourceVersion.objects.filter(is_active=True)

    def initialize(self, request, path_info_segment, **kwargs):
        if request.method in ['GET', 'HEAD']:
            self.permission_classes = (CanViewConceptDictionaryVersion,)
        else:
            self.permission_classes = (CanEditConceptDictionaryVersion,)
        super(SourceVersionBaseView, self).initialize(request, path_info_segment, **kwargs)


class SourceVersionListView(SourceVersionBaseView,
                            mixins.CreateModelMixin,
                            ListWithHeadersMixin):
    released_filter = None
    processing_filter = None
    permission_classes = (CanViewConceptDictionaryVersion,)

    def get(self, request, *args, **kwargs):
        self.serializer_class = SourceVersionDetailSerializer if self.is_verbose(request) else SourceVersionListSerializer
        self.released_filter = parse_boolean_query_param(request, RELEASED_PARAM, self.released_filter)
        self.processing_filter = parse_boolean_query_param(request, PROCESSING_PARAM, self.processing_filter)
        return self.list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.serializer_class = SourceVersionCreateSerializer
        return self.create(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        if not self.versioned_object:
            return HttpResponse(status=status.HTTP_405_METHOD_NOT_ALLOWED)
        serializer = self.get_serializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            self.pre_save(serializer.object)
            self.object = serializer.save(force_insert=True, versioned_object=self.versioned_object)
            if serializer.is_valid():
                self.post_save(self.object, created=True)
                headers = self.get_success_headers(serializer.data)
                serializer = SourceVersionDetailSerializer(self.object, context={'request': request})
                return Response(serializer.data, status=status.HTTP_201_CREATED,
                                headers=headers)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get_queryset(self):
        queryset = super(SourceVersionListView, self).get_queryset()
        if self.processing_filter is not None:
            queryset = queryset.filter(_ocl_processing=self.processing_filter)
        if self.released_filter is not None:
            queryset = queryset.filter(released=self.released_filter)
        return queryset.order_by('-created_at')


class SourceVersionRetrieveUpdateView(SourceVersionBaseView, RetrieveAPIView, UpdateAPIView):
    is_latest = False
    serializer_class = SourceVersionDetailSerializer
    permission_classes = (CanViewConceptDictionaryVersion,)

    def initialize(self, request, path_info_segment, **kwargs):
        self.is_latest = kwargs.pop('is_latest', False)
        super(SourceVersionRetrieveUpdateView, self).initialize(request, path_info_segment, **kwargs)

    def update(self, request, *args, **kwargs):
        self.serializer_class = SourceVersionUpdateSerializer
        if not self.versioned_object:
            return HttpResponse(status=status.HTTP_405_METHOD_NOT_ALLOWED)

        self.object = self.get_object()
        created = False
        save_kwargs = {'force_update': True, 'versioned_object': self.versioned_object}
        success_status_code = status.HTTP_200_OK

        serializer = self.get_serializer(self.object, data=request.DATA,
                                         files=request.FILES, partial=True)

        if serializer.is_valid():
            self.pre_save(serializer.object)
            self.object = serializer.save(**save_kwargs)
            if serializer.is_valid():
                self.post_save(self.object, created=created)
                serializer = SourceVersionDetailSerializer(self.object, context={'request': request})
                return Response(serializer.data, status=success_status_code)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get_object(self, queryset=None):
        if self.is_latest:
            # Determine the base queryset to use.
            if queryset is None:
                queryset = self.filter_queryset(self.get_queryset().order_by('-created_at'))
            else:
                pass  # Deprecation warning

            filter_kwargs = {'released': True}
            obj = get_list_or_404(queryset, **filter_kwargs)[0]

            # May raise a permission denied
            self.check_object_permissions(self.request, obj)
            return obj
        return super(SourceVersionRetrieveUpdateView, self).get_object(queryset)


class SourceVersionRetrieveUpdateDestroyView(SourceVersionRetrieveUpdateView, DestroyAPIView):
    permission_classes = (HasAccessToVersionedObject,)

    def destroy(self, request, *args, **kwargs):
        version = self.get_object()
        if version.released:
            errors = {'non_field_errors' : ['Cannot deactivate a version that is currently released.  Please release another version before deactivating this one.']}
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        return super(SourceVersionRetrieveUpdateDestroyView, self).destroy(request, *args, **kwargs)


class SourceVersionChildListView(ResourceAttributeChildMixin, ListWithHeadersMixin):
    lookup_field = 'version'
    pk_field = 'mnemonic'
    model = SourceVersion
    permission_classes = (HasAccessToVersionedObject,)

    def get(self, request, *args, **kwargs):
        self.serializer_class = SourceVersionDetailSerializer if self.is_verbose(request) else SourceVersionListSerializer
        return self.list(request, *args, **kwargs)

    def get_queryset(self):
        queryset = super(SourceVersionChildListView, self).get_queryset()
        return queryset.filter(parent_version=self.resource_version, is_active=True)


class SourceVersionExportView(ResourceAttributeChildMixin):
    lookup_field = 'version'
    pk_field = 'mnemonic'
    model = SourceVersion
    permission_classes = (CanViewConceptDictionaryVersion,)

    def get(self, request, *args, **kwargs):
        version = self.get_object()
        logger.debug('Export requested for source version %s - Requesting AWS-S3 key' % version)
        key = version.get_export_key()
        url, status = None, 204

        if version.mnemonic == 'HEAD':
            return HttpResponse(status=405)
        if key:
            logger.debug('   Key retreived for source version %s - Generating URL' % version)
            url, status = key.generate_url(60), 200
            logger.debug('   URL retreived for source version %s - Responding to client' % version)
        else:
            logger.debug('   Key does not exist for source version %s' % version)
            status = self.handle_export_source_version()

        response = HttpResponse(status=status)
        response['Location'] = url

        # Set headers to ensure sure response is not cached by a client
        response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        response['lastUpdated'] = version.last_child_update.isoformat()
        response['lastUpdatedTimezone'] = settings.TIME_ZONE
        return response

    def get_queryset(self):
        owner = self.get_owner(self.kwargs)
        queryset = super(SourceVersionExportView, self).get_queryset()
        return queryset.filter(versioned_object_id=Source.objects.get(parent_id=owner.id, mnemonic=self.kwargs['source']).id,
                                            mnemonic=self.kwargs['version'])
    def get_owner(self, kwargs):
        owner = None
        if 'user' in kwargs:
            owner_id = kwargs['user']
            owner = UserProfile.objects.get(mnemonic=owner_id)
        elif 'org' in kwargs:
            owner_id = kwargs['org']
            owner = Organization.objects.get(mnemonic=owner_id)
        return owner

    def post(self, request, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        version = self.get_object()

        if version.mnemonic =='HEAD':
            return HttpResponse(status=405)  # export of head version is not allowed

        logger.debug('Source Export requested for version %s (post)' % version)
        status = 204
        if not version.has_export():
            status = self.handle_export_source_version()
        return HttpResponse(status=status)

    def delete(self, request, *args, **kwargs):
        if not request.user.is_staff:
            return HttpResponseForbidden()
        version = self.get_object()
        if version.has_export():
            key = version.get_export_key()
            key.delete()
        return HttpResponse(status=204)

    def handle_export_source_version(self):
        version = self.get_object()
        try:
            export_source.delay(version.id)
            return 200
        except AlreadyQueued:
            return 204

