# -*- coding: UTF-8 -*-
'''
    nereid.helpers

    Helper utilities

    :copyright: (c) 2010-2012 by Openlabs Technologies & Consulting (P) Ltd.
    :copyright: (c) 2010 by Armin Ronacher.
    :license: BSD, see LICENSE for more details
'''
import os
import posixpath
import mimetypes
from time import time
from zlib import adler32
import re
import urllib
import unicodedata
from functools import wraps
from hashlib import md5
from math import ceil
from tempfile import NamedTemporaryFile

import pytz
from lxml import etree
from lxml.builder import E
from flask.helpers import _assert_have_json, json, jsonify, \
    _PackageBoundObject, locked_cached_property
from werkzeug import Headers, wrap_file, redirect
from werkzeug.exceptions import NotFound
from werkzeug.utils import cached_property

from .globals import session, _request_ctx_stack, current_app, request

from .sphinxapi import SphinxClient

_SLUGIFY_STRIP_RE = re.compile(r'[^\w\s-]')
_SLUGIFY_HYPHENATE_RE = re.compile(r'[-\s]+')

def url_for(endpoint, **values):
    """Generates a URL to the given endpoint with the method provided.
    The endpoint is relative to the active module if modules are in use.

    Here are some examples:

    ==================== ======================= =============================
    Active Module        Target Endpoint         Target Function
    ==================== ======================= =============================
    `None`               ``'index'``             `index` of the application
    `None`               ``'.index'``            `index` of the application
    ``'admin'``          ``'index'``             `index` of the `admin` module
    any                  ``'.index'``            `index` of the application
    any                  ``'admin.index'``       `index` of the `admin` module
    ==================== ======================= =============================

    Variable arguments that are unknown to the target endpoint are appended
    to the generated URL as query arguments.

    For more information, head over to the :ref:`Quickstart <url-building>`.

    :param endpoint: the endpoint of the URL (name of the function)
    :param values: the variable arguments of the URL rule
    :param _external: if set to `True`, an absolute URL is generated.
    :param _secure: if set to `True`, returns an absolute https URL.
    :param _params: if specified as list of tuples or dict, additional url parameters.
            If params is specified then the default behaviour of appending_unknown
            paramters to URL generated is disabled
    """
    ctx = _request_ctx_stack.top
    external = values.pop('_external', False)

    secure = values.pop('_secure', False)
    if secure:
        # A secure url can only be generated on an external url
        external = True

    params = values.pop('_params', None)

    if 'language' not in values:
        values['language'] = request.nereid_language.code
    rv = ctx.url_adapter.build(
        endpoint, values, force_external=external, append_unknown=not params)
    if params:
        params = [(k, v.encode('UTF-8')) for k, v in params]
        rv = u'%s?%s' % (rv, urllib.urlencode(params))
    return (rv.replace('http://', 'https://') if secure else rv)


def secure(function):
    @wraps(function)
    def decorated_function(*args, **kwargs):
        if not request.is_secure:
            return redirect(request.url.replace('http://', 'https://'))
        else:
            return function(*args, **kwargs)
    return decorated_function


def login_required(function):
    @wraps(function)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('nereid.website.login', next=request.url))
        return function(*args, **kwargs)
    return decorated_function


def flash(message, category='message'):
    """Flashes a message to the next request.  In order to remove the
    flashed message from the session and to display it to the user,
    the template has to call :func:`get_flashed_messages`.

    :param message: the message to be flashed.
    :param category: the category for the message.  The following values
                     are recommended: ``'message'`` for any kind of message,
                     ``'error'`` for errors, ``'info'`` for information
                     messages and ``'warning'`` for warnings.  However any
                     kind of string can be used as category.
    """
    session.setdefault('_flashes', []).append((category, unicode(message)))


def get_flashed_messages(with_categories=False):
    """Pulls all flashed messages from the session and returns them.
    Further calls in the same request to the function will return
    the same messages.  By default just the messages are returned,
    but when `with_categories` is set to `True`, the return value will
    be a list of tuples in the form ``(category, message)`` instead.

    Example usage:

    .. sourcecode:: html+jinja

        {% for category, msg in get_flashed_messages(with_categories=true) %}
          <p class=flash-{{ category }}>{{ msg }}
        {% endfor %}

    :param with_categories: set to `True` to also receive categories.
    """
    flashes = _request_ctx_stack.top.flashes
    if flashes is None:
        _request_ctx_stack.top.flashes = flashes = session.pop('_flashes') \
            if '_flashes' in session else []
    if not with_categories:
        return [x[1] for x in flashes]
    return flashes


def send_from_directory(directory, filename, **options):
    """Send a file from a given directory with :func:`send_file`.  This
    is a secure way to quickly expose static files from an upload folder
    or something similar.

    Example usage::

        def download_file(self, filename):
            return send_from_directory(app.config['UPLOAD_FOLDER'],
                                       filename, as_attachment=True)

    .. admonition:: Sending files and Performance

       It is strongly recommended to activate either `X-Sendfile` support in
       your webserver or (if no authentication happens) to tell the webserver
       to serve files for the given path on its own without calling into the
       web application for improved performance.

    :param directory: the directory where all the files are stored.
    :param filename: the filename relative to that directory to
                     download.
    :param options: optional keyword arguments that are directly
                    forwarded to :func:`send_file`.
    """
    filename = posixpath.normpath(filename)
    if filename.startswith(('/', '../')):
        raise NotFound()
    filename = os.path.join(directory, filename)
    if not os.path.isfile(filename):
        raise NotFound()
    return send_file(filename, conditional=True, **options)


def send_file(filename_or_fp, mimetype=None, as_attachment=False,
              attachment_filename=None, add_etags=True,
              cache_timeout=60 * 60 * 12, conditional=False):
    """Sends the contents of a file to the client.  This will use the
    most efficient method available and configured.  By default it will
    try to use the WSGI server's file_wrapper support.  Alternatively
    you can set the application's :attr:`~Flask.use_x_sendfile` attribute
    to ``True`` to directly emit an `X-Sendfile` header.  This however
    requires support of the underlying webserver for `X-Sendfile`.

    By default it will try to guess the mimetype for you, but you can
    also explicitly provide one.  For extra security you probably want
    to sent certain files as attachment (HTML for instance).  The mimetype
    guessing requires a `filename` or an `attachment_filename` to be
    provided.

    Please never pass filenames to this function from user sources without
    checking them first.  Something like this is usually sufficient to
    avoid security problems::

        if '..' in filename or filename.startswith('/'):
            abort(404)

    :param filename_or_fp: the filename of the file to send.  This is
                           relative to the :attr:`~nereid.root_path` if a
                           relative path is specified.
                           Alternatively a file object might be provided
                           in which case `X-Sendfile` might not work and
                           fall back to the traditional method.  Make sure
                           that the file pointer is positioned at the start
                           of data to send before calling :func:`send_file`.
    :param mimetype: the mimetype of the file if provided, otherwise
                     auto detection happens.
    :param as_attachment: set to `True` if you want to send this file with
                          a ``Content-Disposition: attachment`` header.
    :param attachment_filename: the filename for the attachment if it
                                differs from the file's filename.
    :param add_etags: set to `False` to disable attaching of etags.
    :param conditional: set to `True` to enable conditional responses.
    :param cache_timeout: the timeout in seconds for the headers.
    """
    mtime = None
    if isinstance(filename_or_fp, basestring):
        filename = filename_or_fp
        file = None
    else:
        from warnings import warn
        file = filename_or_fp
        filename = getattr(file, 'name', None)

        # XXX: this behaviour is now deprecated because it was unreliable.
        # removed in Flask 1.0
        if not attachment_filename and not mimetype \
           and isinstance(filename, basestring):
            warn(DeprecationWarning('The filename support for file objects '
                'passed to send_file is not deprecated.  Pass an '
                'attach_filename if you want mimetypes to be guessed.'),
                stacklevel=2)
        if add_etags:
            warn(DeprecationWarning('In future flask releases etags will no '
                'longer be generated for file objects passed to the send_file '
                'function because this behaviour was unreliable.  Pass '
                'filenames instead if possible, otherwise attach an etag '
                'yourself based on another value'), stacklevel=2)

    if filename is not None:
        if not os.path.isabs(filename):
            filename = os.path.join(
                current_app.tryton_config['data_path'], 
                current_app.database_name,
                filename)
    if mimetype is None and (filename or attachment_filename):
        mimetype = mimetypes.guess_type(filename or attachment_filename)[0]
    if mimetype is None:
        mimetype = 'application/octet-stream'

    headers = Headers()
    if as_attachment:
        if attachment_filename is None:
            if filename is None:
                raise TypeError('filename unavailable, required for '
                                'sending as attachment')
            attachment_filename = os.path.basename(filename)
        headers.add('Content-Disposition', 'attachment',
                    filename=attachment_filename)

    if current_app.use_x_sendfile and filename:
        if file is not None:
            file.close()
        headers['X-Sendfile'] = filename
        data = None
    else:
        if file is None:
            file = open(filename, 'rb')
            mtime = os.path.getmtime(filename)
        data = wrap_file(request.environ, file)

    rv = current_app.response_class(data, mimetype=mimetype, headers=headers,
                                    direct_passthrough=True)

    # if we know the file modification date, we can store it as the
    # current time to better support conditional requests.  Werkzeug
    # as of 0.6.1 will override this value however in the conditional
    # response with the current time.  This will be fixed in Werkzeug
    # with a new release, however many WSGI servers will still emit
    # a separate date header.
    if mtime is not None:
        rv.date = int(mtime)

    rv.cache_control.public = True
    if cache_timeout:
        rv.cache_control.max_age = cache_timeout
        rv.expires = int(time() + cache_timeout)

    if add_etags and filename is not None:
        rv.set_etag('nereid-%s-%s-%s' % (
            os.path.getmtime(filename),
            os.path.getsize(filename),
            adler32(filename) & 0xffffffff
        ))
        if conditional:
            rv = rv.make_conditional(request)
            # make sure we don't send x-sendfile for servers that
            # ignore the 304 status code for x-sendfile.
            if rv.status_code == 304:
                rv.headers.pop('x-sendfile', None)
    return rv


def slugify(value):
    """
    Normalizes string, converts to lowercase, removes non-alpha characters,
    and converts spaces to hyphens.

    From Django's "django/template/defaultfilters.py".

    Source : http://code.activestate.com/recipes/577257/ (r2)

    >>> slugify('Sharoon Thomas')
    u'sharoon-thomas'
    >>> slugify('Sharoon Thomås')
    u'sharoon-thoms'
    >>> slugify(u'Sharoon Thomås')
    u'sharoon-thomas'
    """
    if not isinstance(value, unicode):
        value = unicode(value, errors='ignore')
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore')
    value = unicode(_SLUGIFY_STRIP_RE.sub('', value).strip().lower())
    return _SLUGIFY_HYPHENATE_RE.sub('-', value)


def _rst_to_html_filter(value):
    """
    Converts RST text to HTML
    ~~~~~~~~~~~~~~~~~~~~~~~~~
    This uses docutils, if the library is missing, then the 
    original text is returned

    Loading to environment::
             from jinja2 import Environment
             env = Environment()
             env.filters['rst'] = rst_to_html
             template = env.from_string("Welcome {{name|rst}}")
             template.render(name="**Sharoon**")
    """
    try:
        from docutils import core
        parts = core.publish_parts(source=value, writer_name='html')
        return parts['body_pre_docinfo'] + parts['fragment']
    except Exception, exc:
        return value


def key_from_list(list_of_args):
    """Builds a key from a list of arguments which could be used for caching
    The key s constructed as an md5 hash
    """
    hash = md5()
    hash.update(repr(list_of_args))
    return hash.hexdigest()


def make_crumbs(browse_record, endpoint, add_home=True, max_depth=10, 
        field_map_changes=None, root_ids=None):
    """Makes bread crumbs for a given browse record based on the field
    parent of the browse record

    :param browse_record: The browse record of the object from which upward 
        tracing of crumbs need to be done
    :param endpoint: The endpoint against which the urls have to be generated
    :param add_home: If provided will add home and home url as the first item
    :param max_depth: Maximum depth of the crumbs
    :param field_map_changes: A dictionary/list of key value pair (tuples) to 
        update the default field_map. Only the changing entries need to be 
        provided.
    :param root_ids: IDs of root nodes where the recursion to a parent node
        will need to be stopped. If not specified the recursion continues
        upto the max_depth. Expects a list or tuple  of ids.

    .. versionchanged:: 0.3
        Added root_ids
    """
    field_map = dict(
        parent_field = 'parent',
        uri_field = 'uri',
        title_field = 'title',
        )
    if field_map_changes is not None:
        field_map.update(field_map_changes)
    if root_ids is None:
        root_ids = tuple()

    def recurse(node, level=1):
        if level > max_depth or not node:
            return []
        data_pair = (
            url_for(endpoint, uri=getattr(node, field_map['uri_field'])),
            getattr(node, field_map['title_field'])
        )
        if node.id in root_ids:
            return [data_pair]
        else:
            return [data_pair] + recurse(
                getattr(node, field_map['parent_field']), level + 1
            )

    items = recurse(browse_record)

    if add_home:
        items.append((url_for('nereid.website.home'), 'Home'))

    # The bread crumb is now in reverse order with home at end, reverse it
    items.reverse()

    return items


class BasePagination(object):
    """
    General purpose paginator for doing pagination

    With an empty dataset assert the attributes
    >>> p = Pagination(1, 3, [])
    >>> p.count
    0
    >>> p.pages
    0
    >>> p.begin_count
    0
    >>> p.end_count
    0

    Test with a range(1, 10)
    >>> p = Pagination(1, 3, range(1, 10))
    >>> p.count
    9
    >>> p.all_items()
    [1, 2, 3, 4, 5, 6, 7, 8, 9]
    >>> p.pages
    3
    >>> p.begin_count
    1
    >>> p.end_count
    3

    """

    def __init__(self, page, per_page, data=None):
        """
        :param per_page: Items per page
        :param page: The page to be displayed
        :param data: The data table
        """
        self.per_page = per_page
        self.page = page
        self.data = data if data is not None else []

    @property
    def count(self):
        "Returns the count of data"
        return len(self.data)

    def all_items(self):
        """Returns complete set of items"""
        return self.data

    def items(self):
        """Returns the list of items in current page
        """
        return self.data[self.offset:self.offset + self.per_page]

    def __iter__(self):
        for item in self.items():
            yield item

    def __len__(self):
        return self.count

    @property
    def prev(self):
        """Returns a :class:`Pagination` object for the previous page."""
        return Pagination(self.page - 1, self.per_page, self.data)

    def next(self):
        """Returns a :class:`Pagination` object for the next page."""
        return Pagination(self.page + 1, self.per_page, self.data)

    #: Attributes below this may not require modifications in general cases

    def iter_pages(self, left_edge=2, left_current=2,
                    right_current=2, right_edge=2):
        """Iterates over the page numbers in the pagination.  The four
        parameters control the thresholds how many numbers should be produced
        from the sides.  Skipped page numbers are represented as `None`.
        This is how you could render such a pagination in the templates:

        .. sourcecode:: html+jinja

            {% macro render_pagination(pagination, endpoint) %}
                <div class=pagination>
                {%- for page in pagination.iter_pages() %}
                    {% if page %}
                        {% if page != pagination.page %}
                            <a href="{{ url_for(endpoint, page=page) }}">{{ page }}</a>
                        {% else %}
                            <strong>{{ page }}</strong>
                        {% endif %}
                    {% else %}
                        <span class=ellipsis>…</span>
                    {% endif %}
                {%- endfor %}
                </div>
            {% endmacro %}
        """
        last = 0
        for num in xrange(1, self.pages + 1):
            if num <= left_edge or \
                (num > self.page - left_current - 1 and \
                 num < self.page + right_current) or \
                num > self.pages - right_edge:
                if last + 1 != num:
                    yield None
                yield num
                last = num

    offset = property(lambda self: (self.page - 1) * self.per_page)

    prev_num = property(lambda self: self.page - 1)
    has_prev = property(lambda self: self.page > 1)

    next_num = property(lambda self: self.page + 1)
    has_next = property(lambda self: self.page < self.pages)

    pages = property(lambda self: int(ceil(self.count / float(self.per_page))))

    begin_count = property(lambda self: min([
        ((self.page - 1) * self.per_page) + 1, 
        self.count]))
    end_count = property(lambda self: min(
        self.begin_count + self.per_page - 1, self.count))


class Pagination(BasePagination):
    """General purpose paginator for doing pagination which can be used by 
    passing a search domain .Remember that this means the query will be built
    and executed and passed on which could be slower than writing native SQL 
    queries. While this fits into most use cases, if you would like to use
    a SQL query rather than a domain use :class:QueryPagination instead
    """

    # The counting of all possible records can be really expensive if you
    # have too many records and the selectivity of the query is low. For 
    # example -  a query to display all products in a website would be quick
    # in displaying the products but slow in building the navigation. So in 
    # cases where this could be frequent, the value of count may be cached and
    # assigned to this variable
    _count = None

    def __init__(self, obj, domain, page, per_page, order=None):
        """
        :param obj: The object itself. pass self within tryton object
        :param domain: Domain for search in tryton
        :param per_page: Items per page
        :param page: The page to be displayed
        """
        self.obj = obj
        self.domain = domain
        self.order = order
        super(Pagination, self).__init__(page, per_page)

    @cached_property
    def count(self):
        """
        Returns the count of entries
        """
        if self.ids_domain():
            return len(self.domain[0][2])
        if self._count is not None:
            return self._count
        return self.obj.search(domain=self.domain, count=True)

    def all_items(self):
        """Returns complete set of items"""
        if self.ids_domain():
            ids = self.domain[0][2]
        else:
            ids = self.obj.search(self.domain)
        return self.obj.browse(ids)

    def ids_domain(self):
        """Returns True if the domain has only IDs and can skip SQL fetch
        to directly browse the records. Else a False is returned
        """
        return (len(self.domain) == 1) and \
            (self.domain[0][0] == 'id') and \
            (self.domain[0][1] == 'in') and \
            (self.order is None)

    def items(self):
        """Returns the list of browse records of items in the page
        """
        if self.ids_domain():
            ids = self.domain[0][2][self.offset:self.offset + self.per_page]
        else:
            ids = self.obj.search(self.domain, offset=self.offset,
                limit=self.per_page, order=self.order)
        return self.obj.browse(ids)

    @property
    def prev(self, error_out=False):
        """Returns a :class:`Pagination` object for the previous page."""
        return self.obj.paginate(self.page - 1, self.per_page, error_out)

    def next(self, error_out=False):
        """Returns a :class:`Pagination` object for the next page."""
        return self.obj.paginate(self.page + 1, self.per_page, error_out)


class QueryPagination(BasePagination):
    """A fast implementation of pagination which uses a SQL query for 
    generating the IDS and hence the pagination"""

    def __init__(self, obj, search_query, count_query, page, per_page):
        """
        :param search_query: Query to be used for search. It must not include
            an OFFSET or LIMIT as they would be automatically added to the 
            query
        :param count_query: Query to be used to get the count of the pagination
            use a query like `SELECT 1234 AS id` for a query where you do not
            want to manipulate the count
        :param per_page: Items per page
        :param page: The page to be displayed
        """
        self.obj = obj
        self.search_query = search_query
        self.count_query = count_query
        super(QueryPagination, self).__init__(page, per_page)

    @cached_property
    def count(self):
        "Return the count of the Items"
        from trytond.transaction import Transaction
        with Transaction().new_cursor() as transaction:
            transaction.cursor.execute(self.count_query)
            return transaction.cursor.fetchone()[0]

    def all_items(self):
        """Returns complete set of items"""
        from trytond.transaction import Transaction
        with Transaction().new_cursor() as transaction:
            transaction.cursor.execute(self.search_query)
            rv = [x[0] for x in transaction.cursor.fetchall()]
        return self.obj.browse(rv)

    def items(self):
        """Returns the list of browse records of items in the page
        """
        from trytond.transaction import Transaction
        limit_string = ' LIMIT %d' % self.per_page
        offset_string = ' OFFSET %d' % self.offset
        with Transaction().new_cursor() as transaction:
            transaction.cursor.execute(''.join([
                self.search_query, limit_string, offset_string
                ]))
            rv = [x[0] for x in transaction.cursor.fetchall()]
        return self.obj.browse(rv)


class SphinxPagination(BasePagination):
    """An implementation of Pagination to be used along with Sphinx Search

    If you need to specify customer filters or range filters you can set that
    on the sphinx_client attribute, which is an instance of SphinxClient. This
    could be done anywhere before rendering or further use of the pagination
    object as the query itself is lazy loaded on first access.

    Example::

        products = SphinxPagination(query, search_index, page, per_page)
        products.sphinx_client.SetFilter("warranty", [1, 2])

    The actual query is only executed when the items are fetched or pagination
    items are called.
    """

    def __init__(self, obj, query, search_index, page, per_page):
        """
        :param obj: The object itself. pass self within tryton object
        :param query: The Query text for pagination
        :param search_index: The search indices in which to look for
        :param page: The current page being displayed
        :param per_page: The number of items per page
        """
        from trytond.config import CONFIG
        if not self.sphinx_enabled():
            raise RuntimeError("Sphinx is not available or configured")

        self.obj = obj
        self.query = query
        self.search_index = search_index
        self.sphinx_client = SphinxClient()
        self.sphinx_client.SetServer(
            CONFIG.options['sphinx_server'], int(CONFIG.options['sphinx_port'])
            )
        super(SphinxPagination, self).__init__(page, per_page)

    def sphinx_enabled(self):
        """A helper method to check if the sphinx client is enabled and 
        configured
        """
        from trytond.config import CONFIG
        return 'sphinx_server' in CONFIG.options and \
            'sphinx_port' in CONFIG.options

    @cached_property
    def count(self):
        "Returns the count of the items"
        return self.result['total_found']

    @cached_property
    def result(self):
        """queries the server and fetches the result. This would only be 
        executed once as this is decorated as a cached property
        """
        # Note: This makes setting limits on the sphinx client basically 
        # useless as it would anyway be set using page and offset just before
        # the query is run
        self.sphinx_client.SetLimits(self.offset, self.per_page)
        rv = self.sphinx_client.Query(self.query, self.search_index)
        if rv is None:
            raise Exception(self.sphinx_client.GetLastError())
        return rv

    def all_items(self):
        """Returns all items. Sphinx by default has a limit of 1000 items
        """
        self.sphinx_client.SetLimit(0, 1000)
        return self.sphinx_client.Query(self.query, self.search_index)

    def items(self):
        """Returns the BrowseRecord of items in the current page"""
        return self.obj.browse(
            [record['id'] for record in self.result['matches']])


class SitemapIndex(object):
    """A collection of Sitemap objects

    To add a sitemap index to one of your objects do the following:

    class Product(ModelSQL, ModelView):
        _name = "product.product"

        def sitemap_index(self):
            index = SitemapIndex(
                self, self.search_domain
                )
            return index.render()

        def sitemap(self, page):
            sitemap_section = SitemapSection(
                self, self.search_domain, page
                )
            return sitemap_section.render()

        def get_absolute_url(self, browse_record, **kwargs):
            "Return the full_path of the current object"
            return url_for('product.product.render', 
                uri=browse_record.uri, **kwargs)
    """
    #: Batch Size: The number of URLs per sitemap page
    batch_size = 1000

    def __init__(self, model, domain, cache_timeout = 60 * 60 * 24):
        """A collection of SitemapSection objects which are automatically
        generated based on the 

        :param collection: An iterable of tuples with the name of the model
            and the domain. Example::

                >>> product_obj = pool.get('product.product')
                >>> sitemap = Sitemap(
                ...     product_obj, [('displayed_on_eshop', '=', True)])
        """
        self.model = model
        self.domain = domain
        self.cache_timeout = cache_timeout

    def render(self):
        with NamedTemporaryFile(suffix=".xml") as buffer:
            buffer.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            buffer.write('<sitemapindex ')
            buffer.write(
                'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            )
            for page in xrange(1, self.page_count + 1):
                loc = '<sitemap><loc>%s</loc></sitemap>\n'
                method = '%s.sitemap' % self.model._name
                buffer.write(loc % url_for(method, page=page, _external=True))
            buffer.write('</sitemapindex>')
            return send_file(buffer.name, cache_timeout=self.cache_timeout)

    @cached_property
    def count(self):
        """Returns the number of items of the object
        """
        from trytond.transaction import Transaction
        with Transaction().new_cursor() as txn:
            query = 'SELECT max(id) FROM "%s"' % self.model._table
            txn.cursor.execute(query)
            max_id, = txn.cursor.fetchone()
            return max_id

    @cached_property
    def page_count(self):
        """Returns the number of pages that will exist for the sitemap index
        >>> int(ceil(1.00/50000.00))
        1
        >>> int(ceil(50000.00/50000.00))
        1
        >>> int(ceil(50001.00/50000.00))
        2
        >>> int(ceil(100000.00/50000.00))
        2
        >>> int(ceil(100001.00/50000.00))
        3
        """
        return int(ceil(float(self.count) / float(self.batch_size)))


class SitemapSection(object):
    """A SitemapSection class is a simple Python class that represents a 
    "section" of  entries in your sitemap. For example, one Sitemap class could
    represent all the entries of your weblog, while another could represent all
    of the events in your events calendar.

    In the simplest case, all these sections get lumped together into one 
    `sitemap.xml`, but it's also possible to use the framework to generate a 
    sitemap index that references individual sitemap files, one per section.

    The implementation though inspired by the Django project, is heavily 
    influenced around the design of the Tryton project.


    How to use::

        Step 1: Create a method name sitemap for each model that needs to use
                a sitemap. Example:

                class Product(ModelSQL, ModelView):
                    _name = "product.product"

                    def sitemap(self, page):
                        sitemap_section = SitemapSection(
                            self, self.search_domain, page
                            )
                        return sitemap_section.render()

                    def get_absolute_url(self, browse_record, **kwargs):
                        "Return the full_path of the current object"
                        return url_for('product.product.render', 
                            uri=browse_record.uri, **kwargs)


    :param model: The Tryton model/object from which the pagination needs to
        be generated. Passing `self` from the calling object's method would be
        the usual way you will have to pass this argument
    :param domain: The domain expression which should be searched against in
        the model
    :param page: The page of the sitemap.
    """

    #: The timeout in seconds for the headers, which would be respceted by
    #: intermediate cache servers.
    cache_timeout = 60 * 60 * 24

    #: Indicates how frequently the page is likely to change. This value 
    #: provides general information to search engines and may not correlate 
    #: exactly to how often they crawl the page. Valid values are:
    #:
    #:     always
    #:     hourly
    #:     daily
    #:     weekly
    #:     monthly
    #:     yearly
    #:     never
    #:
    #: The value "always" should be used to describe documents that change 
    #: each time they are accessed. The value "never" should be used to 
    #: describe archived URLs. Please note that the value of this tag is 
    #: considered a hint and not a command. 
    #:
    #: Even though search engine crawlers may consider this information when 
    #: making decisions, they may crawl pages marked "hourly" less frequently 
    #: than that, and they may crawl pages marked "yearly" more frequently 
    #: than that. Crawlers may periodically crawl pages marked "never" so that
    #: they can handle unexpected changes to those pages.
    #:
    #: Defaults to 'never'
    changefreq = 'never'

    #: The priority of this URL relative to other URLs on your site. Valid 
    #: values range from 0.0 to 1.0. This value does not affect how your pages
    #: are compared to pages on other sitesâ€”it only lets the search engines
    #:  know which pages you deem most important for the crawlers.
    #:
    #: A default priority of 0.5 is assumed by the protocol.
    #:
    #: Please note that the priority you assign to a page is not likely to 
    #: influence the position of your URLs in a search engine's result pages. 
    #: Search engines may use this information when selecting between URLs on 
    #: the same site, so you can use this tag to increase the likelihood that 
    #: your most important pages are present in a search index.
    #:
    #: Also, please note that assigning a high priority to all of the URLs on 
    #: your site is not likely to help you. Since the priority is relative, it
    #: is only used to select between URLs on your site.
    priority = 0.5

    #: It is really memory intensive to call complete records and generate site
    #: maps from them if the collection is large. Hence the queries may be 
    #: divided into batches of this size
    batch_size = 1000

    min_id = property(lambda self: (self.page -1) * self.batch_size)
    max_id = property(lambda self: self.min_id + self.batch_size)

    def __init__(self, model, domain, page):
        self.model = model
        self.domain = domain
        self.page = page

    def __iter__(self):
        """The default implementation searches for the domain and finds the
        ids and generates xml for it
        """
        domain = [('id', '>', self.min_id), ('id', '<=', self.max_id)]
        domain = domain + self.domain

        ids = self.model.search(domain)
        for id in ids:
            record = self.model.browse(id)
            yield(self.get_url_xml(record))
            del record

    def render(self):
        """This method writes the sitemap directly into the response as a stream
        using the ResponseStream
        """
        with NamedTemporaryFile(suffix=".xml") as buffer:
            buffer.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            buffer.write(
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                )
            for line in self:
                buffer.write(etree.tostring(line) + u'\n')
            buffer.write('</urlset>')
            return send_file(buffer.name, cache_timeout=self.cache_timeout)

    def get_url_xml(self, item):
        """Returns the etree node for the specific item
        """
        return E('url',
            E('loc', self.loc(item)),
            E('lastmod', self.lastmod(item)),
            E('changefreq', self.changefreq),
            E('priority', str(self.priority)),
        )

    def loc(self, item):
        """URL of the page. This URL must begin with the protocol 
        (such as http) and end with a trailing slash, if the application 
        requires it. This value must be less than 2,048 characters.

        The URL can probably be generated using url_for and with external
        as True to generate absolute URLs

        Default: returns the absolute url of the object

        :param item: browse_record of the item.
        """
        return self.model.get_absolute_url(item, _external=True)

    def lastmod(self, item):
        """The date of last modification of the file. This date should be in 
        W3C Datetime format. This format allows you to omit the time portion, 
        if desired, and use YYYY-MM-DD. Note that this tag is separate from 
        the If-Modified-Since (304) header the server can return, and 
        search engines may use the information from both sources differently.

        The default implementation fetches the write_date of the record and 
        returns it in the W3C Datetime format

        Note: This attribute is optional and ignored if value is None
        """
        timestamp = item.write_date or item.create_date
        timestamp_in_utc = pytz.utc.localize(timestamp)
        return timestamp_in_utc.isoformat()


class SitemapSectionSQL(SitemapSection):
    """A performance improvement implementation of :class:`SitemapSection` which
    accepts a SQL query instead of a domain expression for high speed data
    recovery in SQL record pagination for large datasets.
    """
    def __init__(self, model, query, page):
        import warnings
        warnings.warn(
            "SQL Sitemap sections will be deprecated", 
            DeprecationWarning
        )
        self.model = model
        self.query = query
        self.page = page

    def __iter__(self):
        """The default implementation searches for the domain and finds the
        ids and generates xml for it
        """
        from trytond.transaction import Transaction
        with Transaction().new_cursor() as txn:
            query = "%s LIMIT %%s OFFSET %%s" % self.query
            txn.cursor.execute(query, (self.limit, self.offset))
            for rec_id, in txn.cursor.cursor:
                 record = self.model.browse(rec_id)
                 yield(self.get_url_xml(record))
                 del record


def get_website_from_host(http_host):
    """Try to find the website name from the HTTP_HOST name"""
    return http_host.split(':')[0]
