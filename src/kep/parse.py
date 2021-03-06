"""Parse tables from raw CSV file using parsing defintion.

   Parsing defintion contains:
   -  parsing boundaries - start and end lines of CSV file segment
   -  link between table headers ("Объем ВВП") and variable names ("GDP")
   -  units of measurement dictionary ("мдрд.руб." -> "bln_rub")

   Parsing procedure:
   - cut out a segment of csv file as delimited by start and end lines (1)
   - save remaining parts of csv file for further parsing      
   - break csv segment into tables, each table containing headers and data rows
   - parse table headers to obtain variable name ("GDP") and unit ("bln_rub")             
   - for tables with varname and unit: 
        split datarows to obtain annual, quarter and monthly values
        emit values as frequency-label-date-value dicts
"""


import warnings
import csv
import re
import itertools
import pandas as pd  
from enum import Enum, unique
from collections import OrderedDict as odict
from datetime import datetime
from calendar import monthrange


import splitter
import cfg
from cfg import spec as SPEC
from cfg import units as UNITS

warnings.simplefilter('ignore', UserWarning)
#warnings.simplefilter('always', UserWarning)

ENC = 'utf-8'
CSV_FORMAT = dict(delimiter='\t', lineterminator='\n')

LABEL_SEP = "__"

# label handling 
def make_label(vn, unit, sep=LABEL_SEP):
    return vn + sep + unit

def split_label(label, sep=LABEL_SEP):
    return label.split(sep)

# csv file access
def to_csv(rows, path):
    """Accept iterable of rows *rows* and write in to *csv_path*"""
    with path.open('w', encoding=ENC) as csvfile:
        filewriter = csv.writer(csvfile, **CSV_FORMAT)
        for row in rows:
            filewriter.writerow(row)
    return path

def from_csv(path):
    """Get iterable of rows from *csv_path*"""
    with path.open(encoding=ENC) as csvfile:
       csvreader = csv.reader(csvfile, **CSV_FORMAT)
       for row in csvreader:
             yield row  
         
def to_dict(row):
    """Make dictionary based on non-empty *row*"""
    if row and row[0]:
       return dict(head=row[0], data=row[1:])
    else:
       return None
    
def read_csv(path):
    """Yield non-empty dictionaries with 'head' and 'data' keys from *path*"""
    raw = from_csv(path)
    csv_dicts = map(to_dict, raw)    
    return filter(lambda x: x is not None, csv_dicts) 

# handling CSV stream

class DictStream():    
    """Holder for CSV rows"""
    def __init__(self, csv_dicts):
        # consume *csv_dicts*, maybe it is a generator
        self.csv_dicts = [x for x in csv_dicts]
    
    @staticmethod
    def is_matched(pat, textline):
        """Return True if *textline* starts with *pat*
           Ignores \" 
        """
        # kill " in both strings
        pat = pat.replace('"','') 
        textline = textline.replace('"','')
        if pat:
            return textline.startswith(pat)
        else:
            return False     
    
    def is_found(self, pat):
        for csv_dict in self.csv_dicts:
            if self.is_matched(pat, csv_dict['head']):
                return True
        return False
          
    def remaining_csv_dicts(self):
        return self.csv_dicts

    def pop(self, pdef):
        # walk by different versions of start/end lines eg 1.10... or 1.9...
        for marker in pdef.markers:
            s = marker['start'] 
            e = marker['end'] 
            if self.is_found(s) and self.is_found(e):
                return self.pop_segment(s, e)
        self.echo_error_ends_not_found(pdef)
        return []    
        
    def echo_error_ends_not_found(self, pdef):
        print("ERROR: start or end line not found in *csv_dicts*")              
        for marker in pdef.markers:
            s = marker['start'] 
            e = marker['end'] 
            print("   ", self.is_found(s), "<{}>".format(s))
            print("   ", self.is_found(e), "<{}>".format(e))                       
    
    def pop_segment(self, start, end):
        """Pops elements of self.csv_dicts between [start, end). 
           Recognises element occurences by index *i*.
           Modifies *self.csv_dicts*."""           
        we_are_in_segment = False
        segment = []
        i = 0
        while i < len(self.csv_dicts):
            row = self.csv_dicts[i]
            line = row['head']
            if self.is_matched(start, line):
                we_are_in_segment = True
            if self.is_matched(end, line):
                break
            if we_are_in_segment:
                segment.append(row)
                del self.csv_dicts[i]
            else:    
                # else is very important, wrong indexing without it
                i += 1
        return segment


# functions for split_to_tables()
YEAR_CATCHER = re.compile('(\d{4}).*')
    
def get_year(string: str, rx=YEAR_CATCHER):
    """Extract year from string *string*. 
       Return None if year is not valid or not in plausible range."""
    match = re.match(rx, string)
    if match:
        year = int(match.group(1))
        if year >= 1991:
            return year
    return None


def is_year(string: str) -> bool:
    return get_year(string) is not None


def is_data_row(row):
    return is_year(row['head'])


@unique
class RowType(Enum):
    UNKNOWN = 0
    DATA = 1
    SECTION = 2
    HEADER = 3

@unique
class State(Enum):
    INIT = 1
    DATA = 2
    UNKNOWN = 3    
    
def split_to_tables(csv_dicts):
    """Yield Table() instances from *csv_dicts* stream."""
    datarows = []
    headers = []
    state = State.INIT
    for d in csv_dicts:        
        if is_data_row(d):
            datarows.append(d)
            state = State.DATA
        else:
            if state == State.DATA: # table ended
                yield Table(headers, datarows)
                headers = []
                datarows = []
            headers.append(d)
            state = State.UNKNOWN
    # still have some data left
    if len(headers) > 0 and len(datarows) > 0:
        yield Table(headers, datarows)

    
      
class Table(): 
    """Holds headers textlines and datarows"""
    
     # [4, 5, 12, 13, 17]
    VALID_ROW_LENGTHS = list(splitter.ROW_LENGTH_TO_FUNC_MAPPER.keys())

    def __init__(self, headers, datarows):
        # WONTFIX: naming deadend with three headers in one line 
        self.header = Header(headers)
        self.datarows = datarows
        self.coln = max([len(d['data']) for d in self.datarows])
        if self.coln not in self.VALID_ROW_LENGTHS:
            """Trying to parse a wierd table without <year> <values> structure. 
               Such tables are currently out of scope of parsing defintion."""
            warnings.warn("Unexpected row length {}\n{}".format(self.coln, self))
                        
    def parse(self, pdef, units):        
        self.header.set_varname(pdef, units)       
        self.header.set_unit(units)
        self.set_splitter(pdef)
        return self           
    
    def set_splitter(self, pdef):
        funcname = pdef.reader
        if funcname:
            self.splitter_func = splitter.get_custom_splitter(funcname)
        else:
            self.splitter_func = splitter.get_splitter(self.coln) 
    
    @property
    def label(self): 
        vn = self.header.varname
        u = self.header.unit
        if vn and u:
            return make_label(vn, u)
        
    def is_defined(self):
        return self.label and self.splitter_func     
    
    def validate(self):
        if not self.label:
            raise ValueError("Label not defined for:\n{}".format(self))
        if not self.splitter_func:
            raise ValueError("Splitter func not defined for:\n{}".format(self))
    
    
    def __str__(self):        
        return "\n".join(["Table {}".format(self.label), 
                          "columns: {}".format(self.coln),
                          self.header.__str__(), 
                          self.repr_datarows()])
    
    def repr_datarows(self):        
        def to_str(row):
           return "{} | {}".format(row['head'], ' '.join(row['data']))
        text = [to_str(row) for row in self.datarows]
        w = max([len(t) for t in text])
        return "\n".join(['-'*w, '\n'.join(text), '-'*w])

    def __repr__(self):
        return "Table {} ".format(self.label) + \
               "({} headers, {} datarows)".format(len(self.header.textlines),
                                                  len(self.datarows))     

class Header():
    """Table header, capable to extract variable label from header textrows."""
    
    KNOWN = "+"
    UNKNOWN = "-"
    
    def __init__(self, csv_dicts):
        self.varname = None
        self.unit = None      
        # ignore comments with "___"
        self.textlines = [d['head'] for d in csv_dicts if not d['head'].startswith("___")]
        self.processed = odict((line, self.UNKNOWN) for line in self.textlines)
        
    def set_varname(self, pdef, units):        
        varname_dict = pdef.headers
        known_headers = varname_dict.keys()
        for line in self.textlines:
            for header in known_headers:
                just_one = 0 
                if header in line:
                    just_one += 1
                    self.processed[line] = self.KNOWN
                    self.varname = varname_dict[header]
                    unit = self.get_unit(line, units)
                    if unit:
                        self.unit = unit
                    else:    
                        """Trying to extract unit of measurement from a header without it.
                           Usually a proper unit is found in next few header textlines."""
                        warnings.warn("unit not found in  <{}>".format(line))
            # anything from known_headers must be found only once         
            assert just_one <= 1
    
    @staticmethod
    def get_unit(line, units):
        for k in units.keys():
            if k in line:  
                return units[k]
        return None 

    def set_unit(self, units):
        for line in self.textlines:            
            unit = self.get_unit(line, units)
            if unit:
                self.unit = unit
                # if unit was found at the start of line mark this line as known
                for u in units.keys():
                   if line.startswith(u):
                      self.processed[line] = self.KNOWN               
    
    def has_unknown_lines(self):
        return self.UNKNOWN in self.processed.values()
    
    def __str__(self):
        show = ["varname: {}, unit: {}".format(self.varname, self.unit)] + \
               ["{} <{}>".format(v,k) for k,v in self.processed.items()]               
        return "\n".join(show)
               

def fix_multitable_units(tables):
    """For tables without *header.varname* copy *header.varname* from previous table.
       Apply to tables that do not have any unknown rows.
    """
    for prev_table, table in zip(tables, tables[1:]):
        if table.header.varname is None:
           if not table.header.has_unknown_lines():
               table.header.varname = prev_table.header.varname

def check_required_labels(tables, pdef):
    labels_required = [make_label(*x) for x in pdef.required]
    labels_in_tables = [t.label for t in tables]
    labels_missed = [x for x in labels_required if x not in labels_in_tables]
    if labels_missed:
        print(labels_missed)
        import pdb; pdb.set_trace()
        raise ValueError(labels_missed)        
    # WONTFIX: not checking if *labels_missed* contains extra parsing results ("_rog", "_yoy", etc)
            
def get_tables(csv_dicts, pdef, units=UNITS):
    tables = [t.parse(pdef, units) for t in split_to_tables(csv_dicts)]
    fix_multitable_units(tables)
    check_required_labels(tables, pdef)
    return tables


def get_all_tables(csv_path, spec=SPEC, units=UNITS):
    csv_dicts = read_csv(csv_path) 
    ds = DictStream(csv_dicts)
    all_tables = []
    # use additional parsing defintions
    for pdef in spec.additional:
        csv_segment = ds.pop(pdef)
        tables = get_tables(csv_segment, pdef, units)
        all_tables.extend(tables)
    # use default parsing defintion
    pdef = spec.main
    csv_segment = ds.remaining_csv_dicts()
    tables = get_tables(csv_segment, pdef, units)
    all_tables.extend(tables)
    return all_tables


def get_all_valid_tables(csv_path, spec=SPEC, units=UNITS):
    all_tables = get_all_tables(csv_path, spec, units)
    return [t for t in all_tables if t.is_defined()]


COMMENT_CATCHER = re.compile("\D*(\d+[.,]?\d*)\s*(?=\d\))")     
            
            
def to_float(text, i=0):
    i += 1
    if i>5:
        raise ValueError("Max recursion depth exceeded on '{}'".format(text))
    if not text:
        return False
    text = text.replace(",", ".")  
    try:
         return float(text)
    except ValueError:
         # note: order of checks important
         if " " in text.strip(): # get first value '542,0 5881)'
             return to_float(text.strip().split(" ")[0], i)
         if ")" in text: # catch '542,01)'
            match_result = COMMENT_CATCHER.match(text)
            if match_result:
                  text = match_result.group(0)
                  return to_float(text, i)
         if text.endswith(".") or text.endswith(",") :  # catch 97.1,
             return to_float(text[:-1], i)
         return False
     
class RowReader():
    
    def __init__(self, label, splitter_func):
        self.label = label
        self.splitter_func = splitter_func
        
    def parse_row(self, year, values):
        a_value, q_values, m_values = self.splitter_func(values)
        a_dict = None
        q_dicts = []
        m_dicts = []
        
        if a_value:
            a_dict = dict(label=self.label,
                          freq='a',
                          year=year, 
                          value=to_float(a_value))
        if q_values:
            for t, val in enumerate(q_values):
                if val:
                    d = dict(label=self.label,
                             freq='q',
                             year=year, 
                             value=to_float(val),
                             qtr=t+1)
                    q_dicts.append(d)
        
        if m_values:
            for t, val in enumerate(m_values):
                if val:
                    d = dict(year=year,
                             label=self.label,
                             freq='m',
                             value=to_float(val),
                             month=t+1)
                    m_dicts.append(d)        
        return a_dict, q_dicts, m_dicts


class Emitter():
    """Emitter extracts and holds annual, quarterly and monthly values 
       for a given Table.
       
       Table must have defined *label* and *splitter_func*."""
        
    def __init__(self, table):
        if not table.label or not table.splitter_func:
            table.validate()
        self.a = []
        self.q = []
        self.m = []        
        rr = RowReader(table.label, table.splitter_func)
        for row in table.datarows:            
            if row['data']:
                year = get_year(row['head'])
                a, q, m = rr.parse_row(year, row['data'])
                if a: 
                    self.a.append(a)
                if q:
                    self.q.extend(q)
                if m:
                    self.m.extend(m)
                
    def emit_a(self):
        return self.a
    
    def emit_q(self):    
        return self.q
    
    def emit_m(self):        
        return self.m
    

class Datapoints():
    def __init__(self, tables):
        self.emitters = [Emitter(t) for t in tables if t.is_defined()]
        self.datapoints = list(self.get_datapoints())
        self.emitters_dict = dict(a=self.emit_a, q=self.emit_q, m=self.emit_m)
        
    def emit_by_method(self, method_name: str):
        if method_name not in ["emit_a", "emit_q", "emit_m"]:
            raise ValueError("Method name not valid: {}".format(method_name)) 
        for e in self.emitters:
            for x in getattr(e, method_name)():                
                    yield x
                        
    def emit_a(self):
        return self.emit_by_method("emit_a")
    
    def emit_q(self):
        return self.emit_by_method("emit_q")

    def emit_m(self):
        return self.emit_by_method("emit_m")
                
    def get(self, freq, label, year):
        assert freq in "aqm"
        gen = self.emitters_dict[freq]()
        gen = filter(lambda x: x['label'].startswith(label), gen)
        return filter(lambda x: x['year'] == year, gen)
    
    def get_datapoints(self):
        return itertools.chain(self.emit_a(), self.emit_q(), self.emit_m())                
        
    def is_included(self, datapoint):
        """Return True if *datapoint* is in *self.datapoints*"""        
        return datapoint in self.datapoints

# short check control values
VALID_DATAPOINTS_SAMPLE =  [
{'freq': 'a', 'label': 'GDP__bln_rub', 'value': 4823.0, 'year': 1999},
{'freq': 'a', 'label': 'GDP__yoy', 'value': 106.4, 'year': 1999},
{'freq': 'a', 'label': 'EXPORT_GOODS_TOTAL__bln_usd', 'value': 75.6, 'year': 1999},
{'freq': 'q', 'label': 'IMPORT_GOODS_TOTAL__bln_usd', 'qtr': 1, 'value': 9.1, 'year': 1999},
{'freq': 'm', 'label': 'RETAIL_SALES_NONFOODS__rog', 'month': 12, 'value': 114.9, 'year': 1999}
]  
 
def approve_csv(year, month, valid_datapoints=VALID_DATAPOINTS_SAMPLE):
    csv_path = cfg.get_path_csv(year,month) 
    print ("File:", csv_path)
    tables = get_all_valid_tables(csv_path)
    dps = Datapoints(tables)
    for x in valid_datapoints:
        if not dps.is_included(x):
            msg1 = "Not found in dataset: {}".format(x)
            msg2 = "Date: {}, {}".format(year, month)
            msg3 = "File: {}".format(csv_path)
            raise ValueError("\n".join([msg1+msg2+msg3]))
    print("Test values parsed OK.") 
    Frame(dps)
    print("Dataframes created OK.") 
    
#dataframe dates handling    
def get_end_of_monthdate(y, m):
    dm = datetime(year=y, month=m, day=monthrange(y, m)[1])
    return pd.Timestamp(dm) 


def get_end_of_quarterdate(y, q):
    dq = datetime(year=y, month=q*3, day=monthrange(y, q*3)[1])
    return pd.Timestamp(dq)  


class Frame():
    """Accept Datapoints() instance and emit pandas DataFrames."""
    
    def __init__(self, datapoints):  
        assert isinstance(datapoints, Datapoints) 
        dfa = pd.DataFrame(datapoints.emit_a())
        dfq = pd.DataFrame(datapoints.emit_q())
        dfm = pd.DataFrame(datapoints.emit_m())       
        for df in dfa, dfq, dfm:
            # df must have no duplicate rows
            assert df[df.duplicated(keep=False)].empty  

        self.dfa = self.reshape_a(dfa) 
        self.dfq = self.reshape_q(dfq) 
        self.dfm = self.reshape_m(dfm) 
        
    @staticmethod
    def reshape_a(dfa):
        """Returns pandas dataframe with ANNUAL data."""
        return dfa.pivot(columns='label', values='value', index='year')        
    
    @staticmethod
    def reshape_q(dfq):
        """Returns pandas dataframe with QUARTERLY data."""
        dfq["time_index"] = dfq.apply(lambda x: get_end_of_quarterdate(x['year'], x['qtr']), axis=1)
        dfq = dfq.pivot(columns='label', values='value', index='time_index')
        dfq.insert(0, "year", dfq.index.year)    
        dfq.insert(1, "qtr", dfq.index.quarter)
        return dfq

    @staticmethod
    def reshape_m(dfm):
        """Returns pandas dataframe with MONTHLY data."""
        dfm["time_index"] = dfm.apply(lambda x: get_end_of_monthdate(x['year'], x['month']), axis=1)
        dfm = dfm.pivot(columns='label', values='value', index='time_index')
        dfm.insert(0, "year", dfm.index.year)
        dfm.insert(1, "month", dfm.index.month)
        return dfm     
     
    def save(self, folder_path):                
        self.dfa.to_csv(folder_path / 'dfa.csv')
        self.dfq.to_csv(folder_path / 'dfq.csv')
        self.dfm.to_csv(folder_path / 'dfm.csv')  
        print("Saved dataframes to", folder_path)


def get_frame(year=None, month=None):
    csv_path = cfg.get_path_csv(year, month)     
    tables = get_all_valid_tables(csv_path)
    dpoints = Datapoints(tables)
    return Frame(dpoints)

    
def dfs(year=None, month=None):
    """Shorthand for obtaining dataframes."""
    frame = get_frame(year, month)  
    return frame.dfa, frame.dfq, frame.dfm


def save_dfs(year=None, month=None):
    """Save dataframes to CSVs.""" 
    frame = get_frame(year, month) 
    processed_folder = cfg.get_processed_folder(year, month)
    frame.save(folder_path=processed_folder)   
    
    
def save_all_dfs():
    for (year, month) in cfg.filled_dates():
         save_dfs(year, month)      


def approve_latest():
    """Quick check for algorithm on latest available data."""
    approve_csv(year=None, month=None)

   
def approve_all(valid_datapoints=VALID_DATAPOINTS_SAMPLE):
    """Check all dates, runs slow (about 20 sec.) 
       May fail if dataset not complete.      
    """
    for (year, month) in cfg.filled_dates():
        approve_csv(year, month, valid_datapoints)


def all_values():
    # emit all values for debugging to_float()
    csv_path = cfg.get_path_csv()
    for t in get_all_tables(csv_path):
         for row in t.datarows:
            for x in row:
               yield x
             
def all_heads():   
     # emit all heads for debugging get_year()
    csv_path = cfg.get_path_csv()          
    csv_dicts = read_csv(csv_path)
    for d in csv_dicts:
         yield d['head']              


def __for_testing__():
    """Holder of boilerplate code for __main__"""
    
    # approve_latest()               
    # approve_all()
    # save_all_dfs()    
    
    # interim to processed data cycle: (year, month) -> 3 dataframes
    year, month = 2017, 4 
    # source csv file
    csv_path = cfg.get_path_csv(year, month)  
    # break csv to tables with variable names
    tables = get_all_valid_tables(csv_path)
    # emit values from tables
    dpoints = Datapoints(tables)    
    # convert stream values to pandas dataframes     
    frame = Frame(datapoints=dpoints)
    # save dataframes to csv files  
    processed_folder = cfg.get_processed_folder(year, month)    
    frame.save(processed_folder)       
    # end of cycle 

if __name__=="__main__":        
    approve_latest()               
    # approve_all()
    # save_all_dfs() 