"""This contains all the views used by the Oplog application."""

# Standard Libraries
import csv
import logging

# Django Imports
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import ListView
from django.views.generic.detail import DetailView, SingleObjectMixin
from django.views.generic.edit import CreateView, DeleteView, UpdateView, View

# 3rd Party Libraries
from tablib import Dataset

# Ghostwriter Libraries
from ghostwriter.api.utils import (
    RoleBasedAccessControlMixin,
    get_logs_list,
    verify_access,
    verify_user_is_privileged,
)
from ghostwriter.oplog.admin import OplogEntryResource
from ghostwriter.oplog.forms import OplogEntryForm, OplogForm
from ghostwriter.oplog.models import Oplog, OplogEntry
from ghostwriter.rolodex.models import Project

# Using __name__ resolves to ghostwriter.oplog.views
logger = logging.getLogger(__name__)


##################
#   AJAX Views   #
##################


class OplogMuteToggle(RoleBasedAccessControlMixin, SingleObjectMixin, View):
    """Toggle the ``mute_notifications`` field of an individual :model:`oplog.Oplog`."""

    model = Oplog

    def test_func(self):
        # Only allow managers and admins to mute notifications
        return verify_user_is_privileged(self.request.user)

    def handle_no_permission(self):
        data = {"result": "error", "message": "Only a manager or admin can mute notifications."}
        return JsonResponse(data, status=403)

    def post(self, *args, **kwargs):
        obj = self.get_object()
        try:
            if obj.mute_notifications:
                obj.mute_notifications = False
                data = {
                    "result": "success",
                    "message": "Log monitor notifications have been unmuted.",
                    "toggle": 0,
                }
            else:
                obj.mute_notifications = True
                data = {
                    "result": "success",
                    "message": "Log monitor notifications have been muted.",
                    "toggle": 1,
                }
            obj.save()
            logger.info(
                "Toggled notifications for %s %s by request of %s",
                obj.__class__.__name__,
                obj.id,
                self.request.user,
            )
        except Exception as exception:  # pragma: no cover
            template = "An exception of type {0} occurred. Arguments:\n{1!r}"
            log_message = template.format(type(exception).__name__, exception.args)
            logger.error(log_message)
            data = {"result": "error", "message": "Could not update mute status for log monitor notifications."}

        return JsonResponse(data)


##################
# View Functions #
##################


@login_required
def oplog_entries_import(request):
    """
    Import a collection of :model:`oplog.OplogEntry` entries for an individual
    :model:`oplog.Oplog`.

    **Template**

    :template:`oplog/oplog_import.html`
    """
    logs = get_logs_list(request.user)
    if request.method == "POST":
        bad_selection = False
        oplog_id = request.POST.get("oplog_id")
        oplog_entry_resource = OplogEntryResource()

        if isinstance(oplog_id, str):
            if oplog_id.isdigit():
                oplog_id = int(oplog_id)
        if oplog_id and isinstance(oplog_id, int):
            try:
                oplog = Oplog.objects.get(id=oplog_id)
                if not verify_access(request.user, oplog.project):
                    bad_selection = True
            except Oplog.DoesNotExist:
                bad_selection = True
        else:
            bad_selection = True

        if bad_selection:
            messages.error(
                request,
                "You selected an invalid log.",
                extra_tags="alert-error",
            )
            return HttpResponseRedirect(reverse("oplog:oplog_import"))

        new_entries = request.FILES["csv_file"].read().decode("iso-8859-1")
        dataset = Dataset()

        imported_data = dataset.load(new_entries, format="csv")
        if not imported_data.headers:
            messages.error(
                request,
                "Your log file is missing the header row.",
                extra_tags="alert-error",
            )
            return HttpResponseRedirect(reverse("oplog:oplog_import"))

        if "oplog_id" in imported_data.headers:
            del imported_data["oplog_id"]
        imported_data.append_col([oplog_id] * len(imported_data), header="oplog_id")

        result = oplog_entry_resource.import_data(imported_data, dry_run=True)
        if result.has_errors():
            row_errors = result.row_errors()
            for exc in row_errors:
                messages.error(
                    request,
                    f"There was an error in row {exc[0]}: {exc[1][0].error}",
                    extra_tags="alert-danger",
                )
            return HttpResponseRedirect(reverse("oplog:oplog_import"))

        oplog_entry_resource.import_data(imported_data, format="csv", dry_run=False)
        messages.success(
            request,
            "Successfully imported log data",
            extra_tags="alert-success",
        )
        return HttpResponseRedirect(reverse("oplog:oplog_entries", kwargs={"pk": oplog_id}))

    return render(request, "oplog/oplog_import.html", context={"logs": logs})


################
# View Classes #
################


class OplogListView(RoleBasedAccessControlMixin, ListView):
    """
    Display a list of :model:`oplog.Oplog`. Only show logs associated with :model:`rolodex.Project`
    to which the user has access.

    **Template**

    :template:`oplog/oplog_list.html`
    """

    model = Oplog
    template_name = "oplog/oplog_list.html"

    def get_queryset(self):
        user = self.request.user
        queryset = get_logs_list(user)
        return queryset


class OplogListEntries(RoleBasedAccessControlMixin, DetailView):
    """
    Display an individual :model:`oplog.Oplog`.

    **Context**

    ``entries``
        :model:`oplog:OplogEntry` entries associated with the :model:`oplog.Oplog`.

    **Template**

    :template:`oplog/oplog_detail.html`
    """

    model = Oplog

    def test_func(self):
        return verify_access(self.request.user, self.get_object().project)

    def handle_no_permission(self):
        messages.error(self.request, "You do not have permission to access that.")
        return redirect("oplog:index")


class OplogCreate(RoleBasedAccessControlMixin, CreateView):
    """
    Create an individual instance of :model:`oplog.Oplog`.

    **Context**

    ``project``
        Instance of :model:`rolodex.Project` associated with this log
    ``cancel_link``
        Link for the form's Cancel button to return to oplog list or details page

    **Template**

    :template:`oplog/oplog_form.html`
    """

    model = Oplog
    form_class = OplogForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        # Check if this request is for a specific project or not
        self.project = ""
        # Determine if ``pk`` is in the kwargs
        if "pk" in self.kwargs:
            pk = self.kwargs.get("pk")
            # Try to get the project from :model:`rolodex.Project`
            if pk:
                try:
                    project = get_object_or_404(Project, pk=self.kwargs.get("pk"))
                    if verify_access(self.request.user, project):
                        self.project = project
                except Project.DoesNotExist:
                    logger.info(
                        "Received log create request for project ID %s, but that project does not exist",
                        pk,
                    )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update({"project": self.project, "user": self.request.user})
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["project"] = self.project
        if self.project:
            ctx["cancel_link"] = reverse("rolodex:project_detail", kwargs={"pk": self.project.pk})
        else:
            ctx["cancel_link"] = reverse("oplog:index")
        return ctx

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if not form.fields["project"].queryset:
            messages.error(
                self.request,
                "There are no active projects for a new activity log.",
                extra_tags="alert-error",
            )
        return form

    def get_initial(self):
        if self.project:
            name = f"{self.project.client} {self.project.project_type} Log"
            return {"name": name, "project": self.project.id}
        return {}

    def get_success_url(self):
        messages.success(
            self.request,
            "Successfully created new operation log",
            extra_tags="alert-success",
        )
        return reverse("oplog:index")


class OplogUpdate(RoleBasedAccessControlMixin, UpdateView):
    """
    Update an individual :model:`oplog.Oplog`.

    **Template**

    :template:`oplog/oplog_form.html`
    """

    model = Oplog
    form_class = OplogForm

    def test_func(self):
        return verify_access(self.request.user, self.get_object().project)

    def handle_no_permission(self):
        messages.error(self.request, "You do not have permission to access that.")
        return redirect("oplog:index")

    def get_success_url(self):
        return reverse("oplog:oplog_entries", args=(self.object.id,))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["cancel_link"] = reverse("oplog:oplog_entries", kwargs={"pk": self.object.pk})
        return ctx

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update({"user": self.request.user})
        return kwargs


class AjaxTemplateMixin:
    def __init__(self):
        pass

    def dispatch(self, request, *args, **kwargs):
        if not hasattr(self, "ajax_template_name"):
            split = self.template_name.split(".html")
            split[-1] = "_inner"
            split.append(".html")
            self.ajax_template_name = "".join(split)
        if request.is_ajax():
            self.template_name = self.ajax_template_name
        return super().dispatch(request, *args, **kwargs)


class OplogEntryCreate(RoleBasedAccessControlMixin, AjaxTemplateMixin, CreateView):
    """
    Create an individual :model:`oplog.OplogEntry`.

    **Template**

    :template:`oplog/oplog_modal.html`
    """

    model = OplogEntry
    form_class = OplogEntryForm
    template_name = "oplog/oplogentry_form.html"
    ajax_template_name = "oplog/snippets/oplogentry_form_inner.html"

    def test_func(self):
        return verify_access(self.request.user, self.get_object().oplog_id.project)

    def handle_no_permission(self):
        messages.error(self.request, "You do not have permission to access that.")
        return redirect("oplog:index")

    def get_success_url(self):
        return reverse("oplog:oplog_entries", args=(self.object.oplog_id.id,))


class OplogEntryUpdate(RoleBasedAccessControlMixin, AjaxTemplateMixin, UpdateView):
    """
    Update an individual :model:`oplog.OplogEntry`.

    **Template**

    :template:`oplog/oplog_modal.html`
    """

    model = OplogEntry
    form_class = OplogEntryForm
    template_name = "oplog/oplogentry_form.html"
    ajax_template_name = "oplog/snippets/oplogentry_form_inner.html"

    def test_func(self):
        return verify_access(self.request.user, self.get_object().oplog_id.project)

    def handle_no_permission(self):
        messages.error(self.request, "You do not have permission to access that.")
        return redirect("oplog:index")

    def get_success_url(self):
        return reverse("oplog:oplog_entries", args=(self.object.oplog_id.id,))


class OplogEntryDelete(RoleBasedAccessControlMixin, DeleteView):
    """
    Delete an individual :model:`oplog.OplogEntry`.
    """

    model = OplogEntry
    fields = "__all__"

    def test_func(self):
        return verify_access(self.request.user, self.get_object().oplog_id.project)

    def handle_no_permission(self):
        messages.error(self.request, "You do not have permission to access that.")
        return redirect("oplog:index")

    def get_success_url(self):
        return reverse("oplog:oplog_entries", args=(self.object.oplog_id.id,))


class OplogExport(RoleBasedAccessControlMixin, SingleObjectMixin, View):
    """Export the :oplog:`oplog.Entries` for an individual :model:`oplog.Oplog` in a csv format."""

    model = Oplog

    def test_func(self):
        return verify_access(self.request.user, self.get_object().project)

    def handle_no_permission(self):
        messages.error(self.request, "You do not have permission to access that.")
        return redirect("oplog:index")

    def get(self, *args, **kwargs):
        obj = self.get_object()

        queryset = obj.entries.all()
        opts = queryset.model._meta

        response = HttpResponse(
            content_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="export.csv"'},
        )

        writer = csv.writer(response)
        field_names = [field.name for field in opts.fields]
        field_names.remove("id")

        # Add the tags field to the list of fields
        field_names.append("tags")

        # Write the headers to the csv file
        writer.writerow(field_names)

        for obj in queryset:
            values = []
            for field in field_names:
                # Special case for oplog_id to write the ID of the oplog instead of the object
                if field == "oplog_id":
                    values.append(getattr(obj, field).id)
                # Special case for tags to write a comma-separated list of tag names
                elif field == "tags":
                    values.append(", ".join([tag.name for tag in obj.tags.all()]))
                else:
                    values.append(getattr(obj, field))
            writer.writerow(values)

        return response
