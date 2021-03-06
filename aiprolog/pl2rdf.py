#!/usr/bin/env python
# -*- coding: utf-8 -*- 

#
# Copyright 2015, 2016, 2017 Guenter Bartsch
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
# helper functions to translate 
# - from prolog logic tree to sparqlalchemy algebra
# - from prolog literals to rdflib literals and back
#

import sys
import datetime
import dateutil.parser
import time
import pytz # $ pip install pytz
import json
import rdflib
from rdflib.plugins.sparql.parserutils import CompValue
import logging

from tzlocal import get_localzone # $ pip install tzlocal

from zamiaprolog.errors  import PrologError, PrologRuntimeError
from zamiaprolog.logic   import NumberLiteral, StringLiteral, ListLiteral, Variable, Predicate, Literal

import model

DT_LIST     = u'http://ai.zamia.org/types/list'
DT_CONSTANT = u'http://ai.zamia.org/types/constant'

class PrologJSONEncoder(json.JSONEncoder):

    def default(self, o):
        
        if isinstance (o, NumberLiteral):
            return {'pt': 'NumberLiteral', 'f': o.f}

        elif isinstance (o, ListLiteral):
            return {'pt': 'ListLiteral', 'l': o.l}
        
        elif isinstance (o, StringLiteral):
            return {'pt': 'StringLiteral', 's': o.s}
        
        elif isinstance (o, Predicate) and len(o.args)==0:
            return {'pt': 'Constant', 'name': o.name}
        

        return json.JSONEncoder.default(self, o)

def _prolog_from_json(o):

    if o['pt'] == 'Constant':
        return Predicate(o['name'])
    if o['pt'] == 'StringLiteral':
        return StringLiteral (o['s'])
    if o['pt'] == 'NumberLiteral':
        return NumberLiteral (o['f'])
    if o['pt'] == 'ListLiteral':
        return ListLiteral (o['l'])

    raise PrologRuntimeError('cannot convert from json: %s .' % repr(o))


def rdf_to_pl(l):

    value    = unicode(l)

    if isinstance (l, rdflib.Literal) :
        if l.datatype:

            datatype = str(l.datatype)

            if datatype == 'http://www.w3.org/2001/XMLSchema#decimal':
                value = NumberLiteral(float(value))
            elif datatype == 'http://www.w3.org/2001/XMLSchema#float':
                value = NumberLiteral(float(value))
            elif datatype == 'http://www.w3.org/2001/XMLSchema#integer':
                value = NumberLiteral(float(value))
            elif datatype == 'http://www.w3.org/2001/XMLSchema#dateTime':
                dt = dateutil.parser.parse(value)
                value = NumberLiteral(time.mktime(dt.timetuple()))
            elif datatype == 'http://www.w3.org/2001/XMLSchema#date':
                dt = dateutil.parser.parse(value)
                value = NumberLiteral(time.mktime(dt.timetuple()))
            elif datatype == DT_LIST:
                value = json.JSONDecoder(object_hook = _prolog_from_json).decode(value)
            elif datatype == DT_CONSTANT:
                value = Predicate (value)
            else:
                raise PrologRuntimeError('sparql_query: unknown datatype %s .' % datatype)
        else:
            if l.value is None:
                value = ListLiteral([])
            else:
                value = StringLiteral(value)
   
    else:
        value = StringLiteral(value)

    return value
    
def pl_literal_to_rdf(a, kb):

    if isinstance (a, NumberLiteral):
        return rdflib.term.Literal (str(a.f), datatype=rdflib.namespace.XSD.decimal)

    if isinstance (a, StringLiteral):
        if a.s.startswith('http://'): # a URL/URI/IRI, apparently
            return rdflib.term.URIRef (a.s)
        return rdflib.term.Literal (a.s)
        
    if isinstance (a, ListLiteral):
        return rdflib.term.Literal (PrologJSONEncoder().encode(a), datatype=DT_LIST)

    if isinstance (a, Predicate):

        if len(a.args) > 0:
            raise PrologError('pl_literal_to_rdf: only constants are supported, found instead: %s (%s)' % (a.__class__, repr(a)))

        name = kb.resolve_aliases_prefixes(a.name)

        if name.startswith('http://'):
            return rdflib.term.URIRef(name)

        return rdflib.term.Literal (a.name, datatype=DT_CONSTANT)

    raise PrologError('pl_literal_to_rdf: unknown argument type: %s (%s)' % (a.__class__, repr(a)))

def pl_to_rdf(term, env, pe, var_map, kb):

    if pe:
        a = pe.prolog_eval(term, env)
    else:
        a = term

    if (not pe or not a) and isinstance (term, Variable):
        if not term.name in var_map:
            var_map[term.name] = rdflib.term.Variable(term.name)
        return var_map[term.name]

    return pl_literal_to_rdf(a, kb)

def _prolog_relational_expression (op, args, env, pe, var_map, kb):

    if len(args) != 2:
        raise PrologError ('_prolog_relational_expression: 2 args expected.')

    expr  = prolog_to_filter_expression (args[0], env, pe, var_map, kb)
    other = prolog_to_filter_expression (args[1], env, pe, var_map, kb)

    return CompValue ('RelationalExpression', 
                      op=op, 
                      expr  = expr,
                      other = other,
                      _vars = set(var_map.values()))

def _prolog_conditional_expression (name, args, env, pe, var_map, kb):

    if len(args) != 2:
        raise PrologError ('_prolog_conditional_expression %s: 2 args expected.' % name)

    return CompValue (name, 
                      expr  = prolog_to_filter_expression (args[0], env, pe, var_map, kb),
                      other = [ prolog_to_filter_expression (args[1], env, pe, var_map, kb) ],
                      _vars = set(var_map.values()))

def prolog_to_filter_expression(e, env, pe, var_map, kb):

    if isinstance (e, Predicate):
    
        if e.name == '=':
            return _prolog_relational_expression ('=', e.args, env, pe, var_map, kb)
        elif e.name == '\=':
            return _prolog_relational_expression ('!=', e.args, env, pe, var_map, kb)
        elif e.name == '<':
            return _prolog_relational_expression ('<', e.args, env, pe, var_map, kb)
        elif e.name == '>':
            return _prolog_relational_expression ('>', e.args, env, pe, var_map, kb)
        elif e.name == '=<':
            return _prolog_relational_expression ('<=', e.args, env, pe, var_map, kb)
        elif e.name == '>=':
            return _prolog_relational_expression ('>=', e.args, env, pe, var_map, kb)
        elif e.name == 'is':
            pre = _prolog_relational_expression ('is', e.args, env, pe, var_map, kb)
            return pre
        elif e.name == 'and':
            return _prolog_conditional_expression ('ConditionalAndExpression', e.args, env, pe, var_map, kb)
        elif e.name == 'or':
            return _prolog_conditional_expression ('ConditionalOrExpression', e.args, env, pe, var_map, kb)
        elif e.name == 'lang':
            if len(e.args) != 1:
                raise PrologError ('lang filter expression: one argument expected.')

            return CompValue ('Builtin_LANG', 
                              arg  = prolog_to_filter_expression (e.args[0], env, pe, var_map, kb),
                              _vars = set(var_map.values()))

    return pl_to_rdf (e, env, pe, var_map, kb)

