# Copyright (c) 2013 Riverbed Technology, Inc.
#
# This software is licensed under the terms and conditions of the 
# MIT License set forth at:
#   https://github.com/riverbed/flyscript-portal/blob/master/LICENSE ("License").  
# This software is distributed "AS IS" as set forth in the License.

import os
import sys
import cgi
import json
import datetime
import importlib
import traceback
import logging

import pytz
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.template import RequestContext
from django.template.defaultfilters import date
from django.shortcuts import render_to_response
from django.core.urlresolvers import reverse
from django.core.servers.basehttp import FileWrapper
from django.core import management
from django.utils.datastructures import SortedDict
from rest_framework import generics, views
from rest_framework.response import Response
from rest_framework.parsers import JSONParser
from rest_framework.renderers import TemplateHTMLRenderer, JSONRenderer

from rvbd_common.apps.datasource.models import Job, Table
from rvbd_common.apps.datasource.serializers import TableSerializer
from rvbd_common.apps.datasource.forms import TableFieldForm
from apps.devices.models import Device
from rvbd_common.apps.report.models import Report, Section, Widget, WidgetJob
from rvbd_common.apps.report.serializers import ReportSerializer
from rvbd_common.apps.report.utils import create_debug_zipfile

logger = logging.getLogger(__name__)


def reload_config(request, report_slug=None):
    """ Reload all reports or one specific report
    """
    if report_slug:
        logger.debug("Reloading %s report" % report_slug)
        management.call_command('reload', report_name=report_slug)
    else:
        logger.debug("Reloading all reports")
        management.call_command('reload')

    if ('HTTP_REFERER' in request.META and
        'reload' not in request.META['HTTP_REFERER']):
        return HttpResponseRedirect(request.META['HTTP_REFERER'])
    else:
        return HttpResponseRedirect(reverse('report-view-root'))


def download_debug(request):
    """ Create zipfile and send it back to client
    """
    # XXX when we implement RBAC this method needs to be ADMIN-level only

    zipfile = create_debug_zipfile()
    wrapper = FileWrapper(file(zipfile))
    response = HttpResponse(wrapper, content_type='application/zip')
    zipname = os.path.basename(zipfile)
    response['Content-Disposition'] = 'attachment; filename=%s' % zipname
    response['Content-Length'] = os.stat(zipfile).st_size
    return response


# token to play with different form types
# currently valid: 'inline' or 'horizontal'
FORMSTYLE = 'horizontal'

class ReportView(views.APIView):
    """ Main handler for /report/{id}
    """
    model = Report
    serializer_class = ReportSerializer
    renderer_classes = (TemplateHTMLRenderer, JSONRenderer)

    def get(self, request, report_slug=None):
        # handle REST calls
        if request.accepted_renderer.format != 'html':
            if report_slug:
                queryset = Report.objects.get(slug=report_slug)
            else:
                queryset = Report.objects.all()
            serializer = ReportSerializer(instance=queryset)
            return Response(serializer.data)

        # handle HTML calls
        try:
            if report_slug is None:
                reports = Report.objects.order_by('position')
                return HttpResponseRedirect(reverse('report-view',
                                                    args=[reports[0].slug]))
            else:
                report = Report.objects.get(slug=report_slug)
        except:
            raise Http404

        logging.debug('Received request for report page: %s' % report_slug)

        # search across all enabled devices
        for device in Device.objects.all():
            if (device.enabled and ('host.or.ip' in device.host or
                                    device.username == '<username>' or
                                    device.password == '<password>' or
                                    device.password == '')):
                return HttpResponseRedirect('%s?invalid=true' %
                                            reverse('device-list'))

        profile = request.user.userprofile
        if not profile.profile_seen:
            # only redirect if first login
            return HttpResponseRedirect(reverse('preferences')+'?next=/report')

        # factory this to make it extensible
        form_init = {'ignore_cache': request.user.userprofile.ignore_cache}

        # Collect all fields organized by section, with section id 0
        # representing common report level fields
        fields_by_section = report.collect_fields_by_section()

        # Merge all the fields into a single dict for use by the Django Form # logic
        all_fields = SortedDict()
        [all_fields.update(c) for c in fields_by_section.values()]
        form = TableFieldForm(all_fields, hidden_fields=report.hidden_fields, initial=form_init)

        # Build a section map that indicates which section each field
        # belongs in when displayed
        section_map = []
        if fields_by_section[0]:
            section_map.append({'title': 'Common', 'parameters': fields_by_section[0]})

        for s in Section.objects.filter(report=report).order_by('position'):
            show = False
            for v in fields_by_section[s.id].values():
                if v.keyword not in (report.hidden_fields or []):
                    show=True
                    break
                
            if show:
                section_map.append({'title': s.title, 'parameters': fields_by_section[s.id]})

        return render_to_response('report.html',
                                  {'report': report,
                                   'developer': profile.developer,
                                   'maps_version': profile.maps_version,
                                   'maps_api_key': profile.maps_api_key,
                                   'endtime' : 'endtime' in form.fields,
                                   'formstyle': FORMSTYLE,
                                   'form': form,
                                   'section_map': section_map,
                                   'show_sections': (len(section_map) > 1) },
                                  context_instance=RequestContext(request))

    def post(self, request, report_slug=None):
        # handle REST calls
        if report_slug is None:
            return self.http_method_not_allowed(request)

        logger.debug("Received POST for report %s, with params: %s" %
                     (report_slug, request.POST))

        try:
            report = Report.objects.get(slug=report_slug)
        except:
            raise Http404

        fields_by_section = report.collect_fields_by_section()
        all_fields = SortedDict()
        [all_fields.update(c) for c in fields_by_section.values()]
        form = TableFieldForm(all_fields, hidden_fields=report.hidden_fields,
                              data=request.POST, files=request.FILES)

        if form.is_valid():

            logger.debug('Form passed validation: %s' % form)
            formdata = form.cleaned_data
            logger.debug('Form cleaned data: %s' % formdata)

            # parse time and localize to user profile timezone
            profile = request.user.userprofile
            timezone = pytz.timezone(profile.timezone)
            form.apply_timezone(timezone)

            if formdata['debug']:
                logger.debug("Debugging report and rotating logs now ...")
                management.call_command('rotate_logs')

            logger.debug("Report %s validated form: %s" %
                         (report_slug, formdata))


            # setup definitions for each Widget
            definition = []

            # store datetime info about when report is being run
            # XXX move datetime format to preferences or somesuch
            now = datetime.datetime.now(timezone)
            definition.append({'datetime': str(date(now, 'jS F Y H:i:s')),
                               'timezone': str(timezone),
                               'debug': formdata['debug']})

            # create matrix of Widgets
            lastrow = -1
            rows = []
            for w in report.widgets().order_by('row', 'col'):
                if w.row != lastrow:
                    lastrow = w.row
                    rows.append([])
                rows[-1].append(Widget.objects.get_subclass(id=w.id))

            # populate definitions
            for row in rows:
                for w in row:
                    widget_def = {"widgettype": w.widgettype().split("."),
                                  "posturl": reverse('widget-job-list',
                                                     args=(report.slug, w.id)),
                                  "options": w.uioptions,
                                  "widgetid": w.id,
                                  "row": w.row,
                                  "width": w.width,
                                  "height": w.height,
                                  "criteria": w.criteria_from_form(form)
                                  }
                    definition.append(widget_def)

            logger.debug("Sending widget definitions for report %s: %s" %
                         (report_slug, definition))

            return HttpResponse(json.dumps(definition))
        else:
            # return form with errors attached in a HTTP 200 Error response
            return HttpResponse(str(form.errors), status=400)


class ReportTableList(generics.ListAPIView):
    model = Table
    serializer_class = TableSerializer

    def get(self, request, *args, **kwargs):
        report = Report.objects.get(slug=kwargs['report_slug'])
        widgets = report.widget_set.all()
        queryset = (table for widget in widgets for table in widget.tables.all())
        serializer = TableSerializer(instance=queryset)
        return Response(serializer.data)


class WidgetJobsList(views.APIView):

    parser_classes = (JSONParser,)

    def post(self, request, report_slug, widget_id, format=None):
        logger.debug("Received POST for report %s, widget %s: %s" %
                     (report_slug, widget_id, request.POST))

        try:
            report = Report.objects.get(slug=report_slug)
            widget = Widget.objects.get(id=widget_id)
        except:
            raise Http404

        req_json = json.loads(request.POST['criteria'])

        fields = widget.collect_fields()

        form = TableFieldForm(fields, use_widgets=False,
                              hidden_fields=report.hidden_fields, include_hidden=True,
                              data=req_json, files=request.FILES)

        if not form.is_valid():
            raise ValueError("Widget internal criteria form is invalid:\n%s" % (form.errors.as_text()))
            
        if form.is_valid():
            logger.debug('Form passed validation: %s' % form)
            formdata = form.cleaned_data
            logger.debug('Form cleaned data: %s' % formdata)

            # parse time and localize to user profile timezone
            profile = request.user.userprofile
            timezone = pytz.timezone(profile.timezone)
            form.apply_timezone(timezone)

            try:
                job = Job.create(table=widget.table(),
                                 criteria=form.criteria())
                job.start()
            
                wjob = WidgetJob(widget=widget, job=job)
                wjob.save()

                logger.debug("Created WidgetJob %s for report %s (handle %s)" %
                             (str(wjob), report_slug, job.handle))

                return Response({"joburl": reverse('report-job-detail',
                                                   args=[report_slug,
                                                         widget_id,
                                                         wjob.id])})
            except Exception as e:
                logger.exception("Failed to start job, an exception occured")
                return HttpResponse(str(e), status=400)

        else:
            logger.error("form is invalid, entering debugger")
            from IPython import embed; embed()


class WidgetJobDetail(views.APIView):

    def get(self, request, report_slug, widget_id, job_id, format=None):
        wjob = WidgetJob.objects.get(id=job_id)

        job = wjob.job
        widget = wjob.widget

        if not job.done():
            # job not yet done, return an empty data structure
            logger.debug("%s: Not done yet, %d%% complete" % (str(wjob),
                                                              job.progress))
            resp = job.json()
        elif job.status == Job.ERROR:
            resp = job.json()
            logger.debug("%s: Job in Error state, deleting Job" % str(wjob))
            wjob.delete()
        else:
            try:
                i = importlib.import_module(widget.module)
                widget_func = i.__dict__[widget.uiwidget].process
                if widget.rows > 0:
                    tabledata = job.values()[:widget.rows]
                else:
                    tabledata = job.values()
                    
                if tabledata is None or len(tabledata) == 0:
                    resp = job.json()
                    resp['status'] = Job.ERROR
                    resp['message'] = "No data returned"
                    logger.debug("%s marked Error: No data returned" % str(wjob))
                elif (hasattr(i, 'authorized') and 
                      not i.authorized(request.user.userprofile)[0]):
                    _, msg = i.authorized(request.user.userprofile)
                    resp = job.json()
                    resp['data'] = None
                    resp['status'] = Job.ERROR
                    resp['message'] = msg
                    logger.debug("%s Error: module unauthorized for user %s"
                                 % (str(wjob), request.user))
                else:
                    data = widget_func(widget, job, tabledata)
                    resp = job.json(data)
                    logger.debug("%s complete" % str(wjob))
            except:
                logger.exception("Widget %s Job %s processing failed" % (job.id, widget.id))
                resp = job.json()
                resp['status'] = Job.ERROR
                ei = sys.exc_info()
                resp['message'] = str(traceback.format_exception_only(ei[0], ei[1]))
            
            wjob.delete()
            
        resp['message'] = cgi.escape(resp['message'])

        return HttpResponse(json.dumps(resp))