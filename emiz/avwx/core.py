"""
Contains the core parsing and indent functions of avwx
"""
# pylint: disable=too-many-branches,too-many-boolean-expressions,too-many-return-statements,bad-continuation
# pylint: disable=inconsistent-return-statements
# stdlib
from copy import copy
from datetime import datetime, timedelta
from itertools import permutations

# library
from dateutil.relativedelta import relativedelta

# module
from .exceptions import BadStation
from .static import (
    CLOUD_LIST, CLOUD_TRANSLATIONS, FLIGHT_RULES, FRACTIONS, IN_REGIONS, METAR_RMK,
    M_IN_REGIONS, M_NA_REGIONS, NA_REGIONS, NUMBER_REPL, SPECIAL_NUMBERS, TAF_NEWLINE, TAF_NEWLINE_STARTSWITH, TAF_RMK,
)
from .structs import Cloud, Fraction, Number, Timestamp, Units


def valid_station(station: str):
    """
    Checks the validity of a station ident

    This function doesn't return anything. It merely raises a BadStation error if needed
    """
    station = station.strip()
    if len(station) != 4:
        raise BadStation('ICAO station idents must be four characters long')
    uses_na_format(station)


def uses_na_format(station: str) -> bool:
    """
    Returns True if the station uses the North American format,
    False if the International format
    """
    if station[0] in NA_REGIONS:
        return True
    if station[0] in IN_REGIONS:
        return False
    if station[:2] in M_NA_REGIONS:
        return True
    if station[:2] in M_IN_REGIONS:
        return False
    raise BadStation("Station doesn't start with a recognized character set")


def is_unknown(val: str) -> bool:
    """
    Returns True if val contains only '/' characters
    """
    for char in ('/', 'X'):
        if val == char * len(val):
            return True
    return False


def unpack_fraction(num: str) -> str:
    """
    Returns unpacked fraction string 5/2 -> 2 1/2
    """
    nums = [int(n) for n in num.split('/') if n]
    if len(nums) == 2 and nums[0] > nums[1]:
        over = nums[0] // nums[1]
        rem = nums[0] % nums[1]
        return f'{over} {rem}/{nums[1]}'
    return num


def remove_leading_zeros(num: str) -> str:
    """
    Strips zeros while handling -, M, and empty strings
    """
    if not num:
        return num
    if num.startswith('M'):
        ret = 'M' + num[1:].lstrip('0')
    elif num.startswith('-'):
        ret = '-' + num[1:].lstrip('0')
    else:
        ret = num.lstrip('0')
    return '0' if ret in ('', 'M', '-') else ret


def spoken_number(num: str) -> str:
    """
    Returns the spoken version of a number

    Ex: 1.2 -> one point two
        1 1/2 -> one and one half
    """
    ret = []
    for part in num.split(' '):
        if part in FRACTIONS:
            ret.append(FRACTIONS[part])
        else:
            ret.append(' '.join([NUMBER_REPL[char] for char in part if char in NUMBER_REPL]))
    return ' and '.join(ret)


def make_number(num: str, repr_: str = None, speak: str = None):
    """
    Returns a Number or Fraction dataclass for a number string
    """
    if not num or is_unknown(num):
        return
    # Check CAVOK
    if num == 'CAVOK':
        return Number('CAVOK', 9999, 'ceiling and visibility ok')  # type: ignore
    # Check special
    if num in SPECIAL_NUMBERS:
        return Number(repr_ or num, None, SPECIAL_NUMBERS[num])  # type: ignore
    # Create Fraction
    if '/' in num:
        nmr, dnm = [int(i) for i in num.split('/')]
        unpacked = unpack_fraction(num)
        spoken = spoken_number(unpacked)
        return Fraction(repr_ or num, nmr / dnm, spoken, nmr, dnm, unpacked)  # type: ignore
    # Create Number
    val = num.replace('M', '-')
    val = float(val) if '.' in num else int(val)  # type: ignore
    return Number(repr_ or num, val, spoken_number(speak or str(val)))  # type: ignore


def find_first_in_list(txt: str, str_list: [str]) -> int:  # type: ignore
    """
    Returns the index of the earliest occurence of an item from a list in a string

    Ex: find_first_in_list('foobar', ['bar', 'fin']) -> 3
    """
    start = len(txt) + 1
    for item in str_list:
        if start > txt.find(item) > -1:
            start = txt.find(item)
    return start if len(txt) + 1 > start > -1 else -1


def get_remarks(txt: str) -> ([str], str):  # type: ignore
    """
    Returns the report split into components and the remarks string

    Remarks can include items like RMK and on, NOSIG and on, and BECMG and on
    """
    txt = txt.replace('?', '').strip()
    # First look for Altimeter in txt
    alt_index = len(txt) + 1
    for item in [' A2', ' A3', ' Q1', ' Q0', ' Q9']:
        index = txt.find(item)
        if len(txt) - 6 > index > -1 and txt[index + 2:index + 6].isdigit():
            alt_index = index
    # Then look for earliest remarks 'signifier'
    sig_index = find_first_in_list(txt, METAR_RMK)
    if sig_index == -1:
        sig_index = len(txt) + 1
    if sig_index > alt_index > -1:
        return txt[:alt_index + 6].strip().split(' '), txt[alt_index + 7:]
    if alt_index > sig_index > -1:
        return txt[:sig_index].strip().split(' '), txt[sig_index + 1:]
    return txt.strip().split(' '), ''


def get_taf_remarks(txt: str) -> (str, str):  # type: ignore
    """
    Returns report and remarks separated if found
    """
    remarks_start = find_first_in_list(txt, TAF_RMK)
    if remarks_start == -1:
        return txt, ''
    remarks = txt[remarks_start:]
    txt = txt[:remarks_start].strip()
    return txt, remarks


STR_REPL = {' C A V O K ': ' CAVOK ', '?': ' '}


def sanitize_report_string(txt: str) -> str:
    """
    Provides sanitization for operations that work better when the report is a string

    Returns the first pass sanitized report string
    """
    if len(txt) < 4:
        return txt
    # Standardize whitespace
    txt = ' '.join(txt.split())
    # Prevent changes to station ID
    stid, txt = txt[:4], txt[4:]
    # Replace invalid key-value pairs
    for key, rep in STR_REPL.items():
        txt = txt.replace(key, rep)
    # Check for missing spaces in front of cloud layers
    # Ex: TSFEW004SCT012FEW///CBBKN080
    for cloud in CLOUD_LIST:
        if cloud in txt and ' ' + cloud not in txt:
            start, counter = 0, 0
            while txt.count(cloud) != txt.count(' ' + cloud):
                cloud_index = start + txt[start:].find(cloud)
                if len(txt[cloud_index:]) >= 3:
                    target = txt[cloud_index + len(cloud):cloud_index + len(cloud) + 3]
                    if target.isdigit() or not target.strip('/'):
                        txt = txt[:cloud_index] + ' ' + txt[cloud_index:]
                start = cloud_index + len(cloud) + 1
                # Prevent infinite loops
                if counter > txt.count(cloud):
                    break
                counter += 1
    return stid + txt


LINE_FIXES = {
    'TEMP0': 'TEMPO', 'TEMP O': 'TEMPO', 'TMPO': 'TEMPO', 'TE MPO': 'TEMPO',
    'TEMP ': 'TEMPO ', 'T EMPO': 'TEMPO', ' EMPO': ' TEMPO', 'TEMO': 'TEMPO',
    'BECM G': 'BECMG', 'BEMCG': 'BECMG', 'BE CMG': 'BECMG', 'B ECMG': 'BECMG',
    ' BEC ': ' BECMG ', 'BCEMG': 'BECMG', 'BEMG': 'BECMG',
}


def sanitize_line(txt: str) -> str:
    """
    Fixes common mistakes with 'new line' signifiers so that they can be recognized
    """
    for key in LINE_FIXES:
        index = txt.find(key)
        if index > -1:
            txt = txt[:index] + LINE_FIXES[key] + txt[index + len(key):]
    # Fix when space is missing following new line signifiers
    for item in ['BECMG', 'TEMPO']:
        if item in txt and item + ' ' not in txt:
            index = txt.find(item) + len(item)
            txt = txt[:index] + ' ' + txt[index:]
    return txt


def extra_space_exists(str1: str, str2: str) -> bool:  # noqa
    """
    Return True if a space shouldn't exist between two items
    """
    ls1, ls2 = len(str1), len(str2)
    if str1.isdigit():
        # 10 SM
        if str2 in ['SM', '0SM']:
            return True
        # 12 /10
        if ls2 > 2 and str2[0] == '/' and str2[1:].isdigit():
            return True
    if str2.isdigit():
        # OVC 040
        if str1 in CLOUD_LIST:
            return True
        # 12/ 10
        if ls1 > 2 and str1.endswith('/') and str1[:-1].isdigit():
            return True
        # 12/1 0
        if ls2 == 1 and ls1 > 3 and str1[:2].isdigit() and '/' in str1 and str1[3:].isdigit():
            return True
        # Q 1001
        if str1 in ['Q', 'A']:
            return True
    # 36010G20 KT
    if str2 == 'KT' and str1[-1].isdigit() \
            and (str1[:5].isdigit() or (str1.startswith('VRB') and str1[3:5].isdigit())):
        return True
    # 36010K T
    if str2 == 'T' and ls1 >= 6 \
            and (str1[:5].isdigit() or (str1.startswith('VRB') and str1[3:5].isdigit())) and str1[-1] == 'K':
        return True
    # OVC022 CB
    if str2 in CLOUD_TRANSLATIONS and str2 not in CLOUD_LIST and ls1 >= 3 and str1[:3] in CLOUD_LIST:
        return True
    # FM 122400
    if str1 in ['FM', 'TL'] and (str2.isdigit() or (str2.endswith('Z') and str2[:-1].isdigit())):
        return True
    # TX 20/10
    if str1 in ['TX', 'TN'] and str2.find('/') != -1:
        return True
    return False


ITEM_REMV = ['AUTO', 'COR', 'NSC', 'NCD', '$', 'KT', 'M', '.', 'RTD', 'SPECI', 'METAR', 'CORR']
ITEM_REPL = {'CALM': '00000KT'}
VIS_PERMUTATIONS = [''.join(p) for p in permutations('P6SM')]
VIS_PERMUTATIONS.remove('6MPS')


def sanitize_report_list(wxdata: [str],  # type: ignore  # noqa
                         remove_clr_and_skc: bool = True
                         ) -> ([str], [str], str):  # type: ignore
    """
    Sanitize wxData

    We can remove and identify "one-off" elements and fix other issues before parsing a line

    We also return the runway visibility and wind shear since they are very easy to recognize
    and their location in the report is non-standard
    """
    shear = ''
    runway_vis = []
    for i, item in reversed(list(enumerate(wxdata))):
        ilen = len(item)
        # Remove elements containing only '/'
        if is_unknown(item):
            wxdata.pop(i)
        # Identify Runway Visibility
        elif ilen > 4 and item[0] == 'R' \
                and (item[3] == '/' or item[4] == '/') and item[1:3].isdigit():
            runway_vis.append(wxdata.pop(i))
        # Remove RE from wx codes, REVCTS -> VCTS
        elif ilen in [4, 6] and item.startswith('RE'):
            wxdata[i] = item[2:]
        # Fix a slew of easily identifiable conditions where a space does not belong
        elif i and extra_space_exists(wxdata[i - 1], item):
            wxdata[i - 1] += wxdata.pop(i)
        # Remove spurious elements
        elif item in ITEM_REMV:
            wxdata.pop(i)
        # Remove 'Sky Clear' from METAR but not TAF
        elif remove_clr_and_skc and item in ['CLR', 'SKC']:
            wxdata.pop(i)
        # Replace certain items
        elif item in ITEM_REPL:
            wxdata[i] = ITEM_REPL[item]
        # Remove ammend signifier from start of report ('CCA', 'CCB',etc)
        elif ilen == 3 and item.startswith('CC') and item[2].isalpha():
            wxdata.pop(i)
        # Identify Wind Shear
        elif ilen > 6 and item.startswith('WS') and item[5] == '/':
            shear = wxdata.pop(i).replace('KT', '')
        # Fix inconsistant 'P6SM' Ex: TP6SM or 6PSM -> P6SM
        elif ilen > 3 and item[-4:] in VIS_PERMUTATIONS:
            wxdata[i] = 'P6SM'
        # Fix wind T
        elif (ilen == 6 and item[5] in ['K', 'T'] and (item[:5].isdigit() or item.startswith('VRB'))) \
                or (ilen == 9 and item[8] in ['K', 'T'] and item[5] == 'G' and (
                item[:5].isdigit() or item.startswith('VRB'))):
            wxdata[i] = item[:-1] + 'KT'
        # Fix joined TX-TN
        elif ilen > 16 and len(item.split('/')) == 3:
            if item.startswith('TX') and 'TN' not in item:
                tn_index = item.find('TN')
                wxdata.insert(i + 1, item[:tn_index])
                wxdata[i] = item[tn_index:]
            elif item.startswith('TN') and item.find('TX') != -1:
                tx_index = item.find('TX')
                wxdata.insert(i + 1, item[:tx_index])
                wxdata[i] = item[tx_index:]
    return wxdata, runway_vis, shear


# pylint: disable=too-many-branches
def get_altimeter(wxdata: [str], units: Units, version: str = 'NA') -> ([str], Number):  # type: ignore  # noqa
    """
    Returns the report list and the removed altimeter item

    Version is 'NA' (North American / default) or 'IN' (International)
    """
    if not wxdata:
        return wxdata, None
    altimeter = ''
    target = wxdata[-1]
    if version == 'NA':
        # Version target
        if target[0] == 'A':
            altimeter = wxdata.pop()[1:]
        # Other version but prefer normal if available
        elif target[0] == 'Q':
            if wxdata[-2][0] == 'A':
                wxdata.pop()
                altimeter = wxdata.pop()[1:]
            else:
                units.altimeter = 'hPa'
                altimeter = wxdata.pop()[1:].lstrip('.')
        # Else grab the digits
        elif len(target) == 4 and target.isdigit():
            altimeter = wxdata.pop()
    elif version == 'IN':
        # Version target
        if target[0] == 'Q':
            altimeter = wxdata.pop()[1:].lstrip('.')
            if '/' in altimeter:
                altimeter = altimeter[:altimeter.find('/')]
        # Other version but prefer normal if available
        elif target[0] == 'A':
            if wxdata[-2][0] == 'Q':
                wxdata.pop()
                altimeter = wxdata.pop()[1:]
            else:
                units.altimeter = 'inHg'
                altimeter = wxdata.pop()[1:]
    # Some stations report both, but we only need one
    if wxdata and (wxdata[-1][0] == 'A' or wxdata[-1][0] == 'Q'):
        wxdata.pop()
    # convert to Number
    if not altimeter:
        return wxdata, None
    if units.altimeter == 'inHg':
        value = altimeter[:2] + '.' + altimeter[2:]
    else:
        value = altimeter
    return wxdata, make_number(value, altimeter)


def get_taf_alt_ice_turb(wxdata: [str]) -> ([str], str, [str], [str]):  # type: ignore
    """
    Returns the report list and removed: Altimeter string, Icing list, Turbulance list
    """
    altimeter = ''
    icing, turbulence = [], []
    for i, item in reversed(list(enumerate(wxdata))):
        if len(item) > 6 and item.startswith('QNH') and item[3:7].isdigit():
            altimeter = wxdata.pop(i)[3:7]
        elif item.isdigit():
            if item[0] == '6':
                icing.append(wxdata.pop(i))
            elif item[0] == '5':
                turbulence.append(wxdata.pop(i))
    return wxdata, altimeter, icing, turbulence


def is_possible_temp(temp: str) -> bool:
    """
    Returns True if all characters are digits or 'M' (for minus)
    """
    for char in temp:
        if not (char.isdigit() or char == 'M'):
            return False
    return True


def get_temp_and_dew(wxdata: str) -> ([str], Number, Number):  # type: ignore
    """
    Returns the report list and removed temperature and dewpoint strings
    """
    for i, item in reversed(list(enumerate(wxdata))):
        if '/' in item:
            # ///07
            if item[0] == '/':
                item = '/' + item.lstrip('/')
            # 07///
            elif item[-1] == '/':
                item = item.rstrip('/') + '/'
            tempdew = item.split('/')
            if len(tempdew) != 2:
                continue
            valid = True
            for j, temp in enumerate(tempdew):
                if temp in ['MM', 'XX']:
                    tempdew[j] = ''
                elif not is_possible_temp(temp):
                    valid = False
                    break
            if valid:
                wxdata.pop(i)  # type: ignore
                return (wxdata, *[make_number(t) for t in tempdew])
    return wxdata, None, None


def get_station_and_time(wxdata: [str]) -> ([str], str, str):  # type: ignore
    """
    Returns the report list and removed station ident and time strings
    """
    station = wxdata.pop(0)
    qtime = wxdata[0]
    if wxdata and qtime.endswith('Z') and qtime[:-1].isdigit():
        rtime = wxdata.pop(0)
    elif wxdata and len(qtime) == 6 and qtime.isdigit():
        rtime = wxdata.pop(0) + 'Z'
    else:
        rtime = ''
    return wxdata, station, rtime


# pylint: disable=too-many-boolean-expressions
def get_wind(wxdata: [str], units: Units) -> ([str], Number, Number, Number, [Number]):  # type: ignore  # noqa
    """
    Returns the report list and removed:
    Direction string, speed string, gust string, variable direction list
    """
    direction, speed, gust = '', '', ''
    variable = []  # type: ignore
    if wxdata:
        item = copy(wxdata[0])
        for rep in ['(E)']:
            item = item.replace(rep, '')
        item = item.replace('O', '0')
        # 09010KT, 09010G15KT
        if item.endswith('KT') \
                or item.endswith('KTS') \
                or item.endswith('MPS') \
                or item.endswith('KMH') \
                or ((len(item) == 5 or (len(item) >= 8 and item.find('G') != -1) and item.find('/') == -1)
                    and (item[:5].isdigit() or (item.startswith('VRB') and item[3:5].isdigit()))):  # noqa
            # In order of frequency
            if item.endswith('KT'):
                item = item.replace('KT', '')
            elif item.endswith('KTS'):
                item = item.replace('KTS', '')
            elif item.endswith('MPS'):
                units.wind_speed = 'm/s'
                item = item.replace('MPS', '')
            elif item.endswith('KMH'):
                units.wind_speed = 'km/h'
                item = item.replace('KMH', '')
            direction = item[:3]
            if 'G' in item:
                g_index = item.find('G')
                gust = item[g_index + 1:]
                speed = item[3:g_index]
            else:
                speed = item[3:]
            wxdata.pop(0)
    # Separated Gust
    if wxdata and 1 < len(wxdata[0]) < 4 and wxdata[0][0] == 'G' and wxdata[0][1:].isdigit():
        gust = wxdata.pop(0)[1:]
    # Variable Wind Direction
    if wxdata and len(wxdata[0]) == 7 and wxdata[0][:3].isdigit() \
            and wxdata[0][3] == 'V' and wxdata[0][4:].isdigit():
        variable = [make_number(i, speak=i) for i in wxdata.pop(0).split('V')]
    # Convert to Number
    direction = make_number(direction, speak=direction)  # type: ignore
    speed = make_number(speed)  # type: ignore
    gust = make_number(gust)  # type: ignore
    return wxdata, direction, speed, gust, variable


def get_visibility(wxdata: [str], units: Units) -> ([str], Number):  # type: ignore
    """
    Returns the report list and removed visibility string
    """
    visibility = ''  # type: ignore
    if wxdata:
        item = copy(wxdata[0])
        # Vis reported in statue miles
        if item.endswith('SM'):  # 10SM
            if item in ('P6SM', 'M1/4SM'):
                visibility = item[:-2]
            elif '/' not in item:
                visibility = str(int(item[:item.find('SM')]))
            else:
                visibility = item[:item.find('SM')]  # 1/2SM
            wxdata.pop(0)
            units.visibility = 'sm'
        # Vis reported in meters
        elif len(item) == 4 and item.isdigit():
            visibility = wxdata.pop(0)
            units.visibility = 'm'
        elif 7 >= len(item) >= 5 and item[:4].isdigit() \
                and (item[4] in ['M', 'N', 'S', 'E', 'W'] or item[4:] == 'NDV'):
            visibility = wxdata.pop(0)[:4]
            units.visibility = 'm'
        elif len(item) == 5 and item[1:].isdigit() and item[0] in ['M', 'P', 'B']:
            visibility = wxdata.pop(0)[1:]
            units.visibility = 'm'
        elif item.endswith('KM') and item[:item.find('KM')].isdigit():
            visibility = item[:item.find('KM')] + '000'
            wxdata.pop(0)
            units.visibility = 'm'
        # Vis statute miles but split Ex: 2 1/2SM
        elif len(wxdata) > 1 and wxdata[1].endswith('SM') and '/' in wxdata[1] and item.isdigit():
            vis1 = wxdata.pop(0)  # 2
            vis2 = wxdata.pop(0).replace('SM', '')  # 1/2
            visibility = str(int(vis1) * int(vis2[2]) + int(vis2[0])) + vis2[1:]  # 5/2
            units.visibility = 'sm'
    return wxdata, make_number(visibility)


def starts_new_line(item: str) -> bool:
    """
    Returns True if the given element should start a new report line
    """
    if item in TAF_NEWLINE:
        return True

    for start in TAF_NEWLINE_STARTSWITH:
        if item.startswith(start):
            return True

    return False


def split_taf(txt: str) -> [str]:  # type: ignore
    """
    Splits a TAF report into each distinct time period
    """
    lines = []
    split = txt.split()
    last_index = 0
    for i, item in enumerate(split):
        if starts_new_line(item) and i != 0 and not split[i - 1].startswith('PROB'):
            lines.append(' '.join(split[last_index:i]))
            last_index = i
    lines.append(' '.join(split[last_index:]))
    return lines


# TAF line report type and start/end times
def get_type_and_times(wxdata: [str]) -> ([str], str, str, str):  # type: ignore
    """
    Returns the report list and removed:
    Report type string, start time string, end time string
    """
    report_type, start_time, end_time = 'FROM', '', ''
    if wxdata:
        # TEMPO, BECMG, INTER
        if wxdata[0] in TAF_NEWLINE:
            report_type = wxdata.pop(0)
        # PROB[30,40]
        elif len(wxdata[0]) == 6 and wxdata[0].startswith('PROB'):
            report_type = wxdata.pop(0)
    if wxdata:
        # 1200/1306
        if len(wxdata[0]) == 9 and wxdata[0][4] == '/' \
                and wxdata[0][:4].isdigit() and wxdata[0][5:].isdigit():
            start_time, end_time = wxdata.pop(0).split('/')
        # FM120000
        elif len(wxdata[0]) > 7 and wxdata[0].startswith('FM'):
            report_type = 'FROM'
            if '/' in wxdata[0] and wxdata[0][2:].split('/')[0].isdigit() \
                    and wxdata[0][2:].split('/')[1].isdigit():
                start_time, end_time = wxdata.pop(0)[2:].split('/')
            elif wxdata[0][2:8].isdigit():
                start_time = wxdata.pop(0)[2:6]
            # TL120600
            if wxdata and len(wxdata[0]) > 7 and wxdata[0].startswith('TL') \
                    and wxdata[0][2:8].isdigit():
                end_time = wxdata.pop(0)[2:6]
    return wxdata, report_type, start_time, end_time


def _is_tempo_or_prob(report_type: str) -> bool:
    """
    Returns True if report type is TEMPO or PROB__
    """
    if report_type == 'TEMPO':
        return True
    if len(report_type) == 6 and report_type.startswith('PROB'):
        return True
    return False


def _get_next_time(lines: [dict], target: str) -> str:  # type: ignore
    """
    Returns the next FROM target value or empty
    """
    for line in lines:
        if line[target] and not _is_tempo_or_prob(line['type']):
            return line[target]
    return ''


def find_missing_taf_times(lines: [dict], start: Timestamp, end: Timestamp) -> [dict]:  # type: ignore
    """
    Fix any missing time issues (except for error/empty lines)
    """
    if not lines:
        return lines
    # Assign start time
    lines[0]['start_time'] = start
    # Fix other times
    last_fm_line = 0
    for i, line in enumerate(lines):
        if _is_tempo_or_prob(line['type']):
            continue
        last_fm_line = i
        # Search remaining lines to fill empty end or previous for empty start
        for target, other, direc in (('start', 'end', -1), ('end', 'start', 1)):
            target += '_time'
            if not line[target]:
                line[target] = _get_next_time(lines[i::direc][1:], other + '_time')
    # Special case for final forcast
    if last_fm_line:
        lines[last_fm_line]['end_time'] = end
    # Reset original end time if still empty
    if lines and not lines[0]['end_time']:
        lines[0]['end_time'] = end
    return lines


def get_temp_min_and_max(wxlist: [str]) -> ([str], str, str):  # type: ignore
    """
    Pull out Max temp at time and Min temp at time items from wx list
    """
    temp_max, temp_min = '', ''
    for i, item in reversed(list(enumerate(wxlist))):
        if len(item) > 6 and item[0] == 'T' and '/' in item:
            # TX12/1316Z
            if item[1] == 'X':
                temp_max = wxlist.pop(i)
            # TNM03/1404Z
            elif item[1] == 'N':
                temp_min = wxlist.pop(i)
            # TM03/1404Z T12/1316Z -> Will fix TN/TX
            elif item[1] == 'M' or item[1].isdigit():
                if temp_min:
                    if int(temp_min[2:temp_min.find('/')].replace('M', '-')) \
                            > int(item[1:item.find('/')].replace('M', '-')):
                        temp_max = 'TX' + temp_min[2:]
                        temp_min = 'TN' + item[1:]
                    else:
                        temp_max = 'TX' + item[1:]
                else:
                    temp_min = 'TN' + item[1:]
                wxlist.pop(i)
    return wxlist, temp_max, temp_min


def _get_digit_list(alist: [str], from_index: int) -> ([str], [str]):  # type: ignore
    """
    Returns a list of items removed from a given list of strings
    that are all digits from 'from_index' until hitting a non-digit item
    """
    ret = []
    alist.pop(from_index)
    while len(alist) > from_index and alist[from_index].isdigit():
        ret.append(alist.pop(from_index))
    return alist, ret


def get_oceania_temp_and_alt(wxlist: [str]) -> ([str], [str], [str]):  # type: ignore
    """
    Get Temperature and Altimeter lists for Oceania TAFs
    """
    tlist, qlist = [], []  # type: ignore
    if 'T' in wxlist:
        wxlist, tlist = _get_digit_list(wxlist, wxlist.index('T'))
    if 'Q' in wxlist:
        wxlist, qlist = _get_digit_list(wxlist, wxlist.index('Q'))
    return wxlist, tlist, qlist


def sanitize_cloud(cloud: str) -> str:
    """
    Fix rare cloud layer issues
    """
    if len(cloud) < 4:
        return cloud
    if not cloud[3].isdigit() and cloud[3] != '/':
        if cloud[3] == 'O':
            cloud = cloud[:3] + '0' + cloud[4:]  # Bad "O": FEWO03 -> FEW003
        else:  # Move modifiers to end: BKNC015 -> BKN015C
            cloud = cloud[:3] + cloud[4:] + cloud[3]
    return cloud


def split_cloud(cloud: str) -> [str]:  # type: ignore
    """
    Transforms a cloud string into a list of strings: [Type, Height (, Optional Modifier)]
    """
    split = []
    cloud = sanitize_cloud(cloud)
    if cloud.startswith('VV'):
        split.append(cloud[:2])
        cloud = cloud[2:]
    while len(cloud) >= 3:
        split.append(cloud[:3])
        cloud = cloud[3:]
    if cloud:
        split.append(cloud)
    # Nullify unknown elements
    for i, item in enumerate(split):
        if is_unknown(item):
            split[i] = None  # type: ignore
    # Add null altitude or convert to int
    if len(split) == 1:
        split.append(None)  # type: ignore
    elif isinstance(split[1], str) and split[1].isdigit():
        split[1] = int(split[1])  # type: ignore
    return split


def make_cloud(cloud: str) -> Cloud:
    """
    Returns a Cloud dataclass for a cloud string

    This function assumes the input is potentially valid
    """
    return Cloud(cloud, *split_cloud(cloud))  # type: ignore


def get_clouds(wxdata: [str]) -> ([str], list):  # type: ignore
    """
    Returns the report list and removed list of split cloud layers
    """
    clouds = []
    for i, item in reversed(list(enumerate(wxdata))):
        if item[:3] in CLOUD_LIST or item[:2] == 'VV':
            cloud = wxdata.pop(i)
            clouds.append(make_cloud(cloud))
    return wxdata, sorted(clouds, key=lambda cloud: (cloud.altitude, cloud.type))


def get_flight_rules(vis: Number, ceiling: Cloud) -> int:
    """
    Returns int based on current flight rules from parsed METAR data

    0=VFR, 1=MVFR, 2=IFR, 3=LIFR

    Note: Common practice is to report IFR if visibility unavailable
    """
    # Parse visibility
    if not vis:
        return 2
    if vis.repr == 'CAVOK' or vis.repr.startswith('P6'):
        vis = 10  # type: ignore
    elif vis.repr.startswith('M'):
        vis = 0  # type: ignore
    # Convert meters to miles
    elif len(vis.repr) == 4:
        vis = vis.value * 0.000621371  # type: ignore
    else:
        vis = vis.value  # type: ignore
    # Parse ceiling
    cld = ceiling.altitude if ceiling else 99
    # Determine flight rules
    if (vis <= 5) or (cld <= 30):  # type: ignore
        if (vis < 3) or (cld < 10):  # type: ignore
            if (vis < 1) or (cld < 5):  # type: ignore
                return 3  # LIFR
            return 2  # IFR
        return 1  # MVFR
    return 0  # VFR


def get_taf_flight_rules(lines: [dict]) -> [dict]:  # type: ignore
    """
    Get flight rules by looking for missing data in prior reports
    """
    for i, line in enumerate(lines):
        temp_vis, temp_cloud = line['visibility'], line['clouds']
        for report in reversed(lines[:i]):
            if not _is_tempo_or_prob(report['type']):
                if temp_vis == '':
                    temp_vis = report['visibility']
                if 'SKC' in report['other'] or 'CLR' in report['other']:
                    temp_cloud = 'temp-clear'
                elif temp_cloud == []:
                    temp_cloud = report['clouds']
                if temp_vis != '' and temp_cloud != []:
                    break
        if temp_cloud == 'temp-clear':
            temp_cloud = []
        line['flight_rules'] = FLIGHT_RULES[get_flight_rules(temp_vis, get_ceiling(temp_cloud))]
    return lines


def get_ceiling(clouds: [Cloud]) -> Cloud:  # type: ignore
    """
    Returns ceiling layer from Cloud-List or None if none found

    Assumes that the clouds are already sorted lowest to highest

    Only 'Broken', 'Overcast', and 'Vertical Visibility' are considdered ceilings

    Prevents errors due to lack of cloud information (eg. '' or 'FEW///')
    """
    for cloud in clouds:
        if cloud.altitude and cloud.type in ('OVC', 'BKN', 'VV'):
            return cloud
    return None  # type: ignore


def parse_date(date: str, hour_threshold: int = 200):
    """
    Parses a report timestamp in ddhhZ or ddhhmmZ format

    This function assumes the given timestamp is within the hour threshold from current date
    """
    # Format date string
    date = date.strip('Z')
    if len(date) == 4:
        date += '00'
    if not (len(date) == 6 and date.isdigit()):
        return
    # Create initial guess
    now = datetime.utcnow()
    guess = now.replace(day=int(date[0:2]),
                        hour=int(date[2:4]) % 24,
                        minute=int(date[4:6]) % 60,
                        second=0, microsecond=0)
    hourdiff = (guess - now) / timedelta(minutes=1) / 60
    # Handle changing months
    if hourdiff > hour_threshold:
        guess += relativedelta(months=-1)
    elif hourdiff < -hour_threshold:
        guess += relativedelta(months=+1)
    return guess


def make_timestamp(timestamp: str) -> Timestamp:
    """
    Returns a Timestamp dataclass for a report timestamp in ddhhZ or ddhhmmZ format
    """
    return Timestamp(timestamp, parse_date(timestamp))  # type: ignore  # type: ignore
